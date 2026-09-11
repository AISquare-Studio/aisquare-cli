"""What the operation journal promises, asserted one property at a time.

The scenarios are the ones P06 is accepted against: migration 002 arriving
through P02's seam without disturbing 001, a reservation that is committed
before anything else can happen, a fingerprint that tells an identical retry
from a conflicting reuse, compare-and-set transitions that two workers cannot
both win, a restart that settles unfinished rows without repeating a side
effect, and text that cannot carry a path or a credential out of the database.

Every test builds its own sidecar under ``tmp_path``. Nothing here reads a real
home, spawns an agent, opens a socket or calls an executor — the journal is a
file and a state machine, and that is the whole surface under test.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aisquare.office.config import OfficeConfig
from aisquare.office.models import (
    ActionContext,
    ActionSpec,
    KeysRequest,
    OperationTarget,
    Receipt,
    ServiceError,
    StopRequest,
    TellRequest,
)
from aisquare.office.operation_store import (
    LEGACY_ACTION_KINDS,
    OPERATION_LOCK_TABLE,
    OPERATION_TABLE,
    WIRE_OPERATION_KINDS,
    IllegalTransition,
    OperationError,
    OperationEvent,
    OperationJournal,
    OperationLimits,
    OperationRecord,
    Reconciliation,
    UnknownOperation,
    canonical_fingerprint,
    unknown_error,
)
from aisquare.office.ports import OperationStore
from aisquare.office.storage import ObservationDatabase
from aisquare.office.storage_schema import MIGRATIONS, OBSERVATION_TABLES, SCHEMA_TABLE

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


class FrozenClock:
    """Wall time a test sets, and a monotonic clock a test advances."""

    def __init__(self, now: datetime = NOW) -> None:
        self._now = now
        self._monotonic = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def set(self, now: datetime) -> None:
        self._now = now

    def advance(self, seconds: float) -> None:
        self._monotonic += seconds


def _journal(
    home: Path, clock: FrozenClock | None = None, limits: OperationLimits | None = None
) -> OperationJournal:
    database = ObservationDatabase.from_config(OfficeConfig(home=home), clock or FrozenClock())
    return OperationJournal(database, clock or FrozenClock(), limits)


def _context(
    *,
    scope: str = "local:session-a",
    key: str | None = "idem-1",
    revision: str = "1.4",
) -> ActionContext:
    return ActionContext(
        auth_scope=scope,
        received_at=NOW,
        contract_revision="1.4" if revision == "1.4" else "1.3",
        request_id="req-1",
        idempotency_key=key,
    )


def _spec(
    kind: str = "agent.tell",
    *,
    agent_id: str | None = "agt-1",
    project_id: str | None = None,
    text: str = "status please",
) -> ActionSpec:
    return ActionSpec(
        kind=kind,  # type: ignore[arg-type]
        target=OperationTarget(project_id=project_id, agent_id=agent_id),
        body=TellRequest(text=text),
    )


def _receipt(detail: str = "typed into the pane") -> Receipt:
    return Receipt(delivered="typed", detail=detail, agent_id="agt-1", at=NOW)


def _tables(path: Path) -> set[str]:
    connection = sqlite3.connect(str(path))
    try:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    finally:
        connection.close()
    return {row[0] for row in rows}


def _rows(path: Path) -> list[sqlite3.Row]:
    """Read the journal through an INDEPENDENT connection.

    Independent on purpose: a row this connection can see is a row that was
    committed, which is the property "reserve before any side effect" reduces
    to. Reading through the journal's own object would prove nothing about the
    transaction having closed.

    An absent file is an empty journal, and saying so here is not a convenience:
    a refusal that happens before any reservation never opens the sidecar at
    all, so "no rows" and "no database" are the same answer to the question
    these tests ask.
    """
    if not path.exists():
        return []
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        return list(connection.execute(f"SELECT * FROM {OPERATION_TABLE}").fetchall())
    finally:
        connection.close()


# -- migration 002 ---------------------------------------------------------


def test_migration_002_is_registered_through_p02s_seam_at_import() -> None:
    """P06 appends to P02's registry rather than editing P02's module."""
    registered = {migration.version: migration.name for migration in MIGRATIONS}
    versions = [migration.version for migration in MIGRATIONS]

    assert registered[1] == "observations"
    assert registered[2] == "operations"
    assert versions == sorted(set(versions)), "versions must be unique and strictly increasing"


def test_a_database_with_002_registered_reaches_version_two_exactly(tmp_path: Path) -> None:
    """The exact-version claim P02's test used to make, now owned by P06.

    ``test_observation_storage`` compares against ``MIGRATIONS.latest_version()``
    so that it survives migration 003; the literal 2 lives here, in the packet
    that ships 002, so the number is still asserted somewhere.
    """
    database = ObservationDatabase.from_config(OfficeConfig(home=tmp_path / "home"), FrozenClock())

    version = database.migrate()

    assert version == 2
    assert database.schema_version() == 2


def test_migration_002_creates_the_operation_tables_and_leaves_001s_alone(
    tmp_path: Path,
) -> None:
    database = ObservationDatabase.from_config(OfficeConfig(home=tmp_path / "home"), FrozenClock())
    database.migrate()

    tables = _tables(database.path)

    assert OPERATION_TABLE in tables
    assert OPERATION_LOCK_TABLE in tables
    assert set(OBSERVATION_TABLES) <= tables, "migration 002 must not disturb 001's tables"
    assert SCHEMA_TABLE in tables
    assert OPERATION_TABLE not in OBSERVATION_TABLES


def test_the_unique_index_is_the_key_pair_and_nothing_else(tmp_path: Path) -> None:
    """Widening it with kind or target is how conflicting reuse slips past.

    Asserted against the schema itself rather than behaviour, because a second
    column added here would still pass every idempotency test — it would only
    stop REFUSING the case those tests do not construct.
    """
    database = ObservationDatabase.from_config(OfficeConfig(home=tmp_path / "home"), FrozenClock())
    database.migrate()

    connection = sqlite3.connect(str(database.path))
    try:
        columns = connection.execute("PRAGMA index_info(ux_operation_key)").fetchall()
        names = [row[2] for row in columns]
    finally:
        connection.close()

    assert names == ["auth_scope", "idempotency_key"]


# -- reservation -----------------------------------------------------------


def test_a_reservation_is_committed_before_the_caller_is_told_anything(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "home")

    reservation = journal.reserve(_spec(), _context())

    assert reservation.disposition == "new"
    assert reservation.record is not None
    committed = _rows(journal.database_path)
    assert [row["operation_id"] for row in committed] == [reservation.record.operation_id]
    assert committed[0]["status"] == "queued"


def test_an_identical_retry_returns_the_existing_record(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "home")
    first = journal.reserve(_spec(), _context())

    second = journal.reserve(_spec(), _context())

    assert second.disposition == "existing"
    assert first.record is not None
    assert second.record is not None
    assert second.record.operation_id == first.record.operation_id
    assert len(_rows(journal.database_path)) == 1


@pytest.mark.parametrize(
    ("what", "spec"),
    [
        ("body", _spec(text="something else entirely")),
        ("kind", _spec("agent.interrupt")),
        ("target", _spec(agent_id="agt-2")),
    ],
)
def test_the_same_key_with_a_different_fingerprint_is_a_conflict(
    tmp_path: Path, what: str, spec: ActionSpec
) -> None:
    """Kind, target and body are all in the fingerprint, so all three conflict."""
    journal = _journal(tmp_path / "home")
    journal.reserve(_spec(), _context())

    reservation = journal.reserve(spec, _context())

    assert reservation.disposition == "conflict", f"a changed {what} must not reuse a reservation"
    assert reservation.record is None, "a conflict carries no record to return"
    assert len(_rows(journal.database_path)) == 1


def test_a_conflict_is_detected_before_the_existing_status_is_consulted(tmp_path: Path) -> None:
    """A settled result must never be handed to a conflicting reuse."""
    journal = _journal(tmp_path / "home")
    first = journal.reserve(_spec(), _context())
    assert first.record is not None
    journal.apply_transition(first.record.operation_id, "queued", "running")
    journal.apply_transition(first.record.operation_id, "running", "succeeded", _receipt())

    reservation = journal.reserve(_spec(text="a different instruction"), _context())

    assert reservation.disposition == "conflict"
    assert reservation.record is None


def test_the_same_key_in_another_auth_scope_is_a_separate_operation(tmp_path: Path) -> None:
    """Uniqueness is the pair, so two sessions cannot collide on a common key."""
    journal = _journal(tmp_path / "home")
    mine = journal.reserve(_spec(), _context(scope="local:session-a"))

    theirs = journal.reserve(_spec(), _context(scope="local:session-b"))

    assert mine.record is not None
    assert theirs.record is not None
    assert theirs.disposition == "new"
    assert theirs.record.operation_id != mine.record.operation_id
    assert len(_rows(journal.database_path)) == 2


def test_a_mutation_without_a_resolved_key_never_reaches_the_journal(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "home")

    with pytest.raises(OperationError, match="resolved idempotency key"):
        journal.reserve(_spec(), _context(key=None))

    assert _rows(journal.database_path) == []


def test_the_auth_scope_is_stored_as_a_digest_not_in_the_clear(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "home")

    journal.reserve(_spec(), _context(scope="local:session-secret"))

    stored = _rows(journal.database_path)[0]
    assert "session-secret" not in str(stored["auth_scope"])
    assert len(str(stored["auth_scope"])) == 64


# -- the fingerprint -------------------------------------------------------


def test_an_absent_field_and_an_explicit_null_hash_the_same(tmp_path: Path) -> None:
    """One canonical representation, so a client's spelling cannot look like a conflict."""
    absent = ActionSpec(kind="agent.spawn", target=OperationTarget(project_id="prj-1"))
    explicit = ActionSpec(
        kind="agent.spawn", target=OperationTarget(project_id="prj-1", agent_id=None)
    )

    assert canonical_fingerprint(absent) == canonical_fingerprint(explicit)


def test_two_body_types_with_identical_values_do_not_share_a_fingerprint() -> None:
    """The body's TYPE is hashed too, or one key could span two actions."""
    empty_stop = ActionSpec(kind="agent.stop", target=OperationTarget(agent_id="agt-1"))
    typed_stop = ActionSpec(
        kind="agent.stop", target=OperationTarget(agent_id="agt-1"), body=StopRequest()
    )

    assert canonical_fingerprint(empty_stop) != canonical_fingerprint(typed_stop)


def test_list_order_matters_and_object_key_order_does_not() -> None:
    first = ActionSpec(
        kind="terminal.keys",
        target=OperationTarget(agent_id="agt-1"),
        body=KeysRequest(keys=("a", "b")),
    )
    reordered = ActionSpec(
        kind="terminal.keys",
        target=OperationTarget(agent_id="agt-1"),
        body=KeysRequest(keys=("b", "a")),
    )
    same = ActionSpec(
        kind="terminal.keys",
        target=OperationTarget(agent_id="agt-1"),
        body=KeysRequest(keys=("a", "b")),
    )

    assert canonical_fingerprint(first) != canonical_fingerprint(reordered)
    assert canonical_fingerprint(first) == canonical_fingerprint(same)


def test_a_body_that_is_not_a_validated_model_is_refused_by_the_spec() -> None:
    """P01's guard, asserted here because P06 is what would execute the result."""
    with pytest.raises(TypeError, match="validated OfficeModel"):
        ActionSpec(
            kind="agent.tell",
            target=OperationTarget(agent_id="agt-1"),
            body={"text": "raw input"},  # type: ignore[arg-type]
        )


# -- concurrency -----------------------------------------------------------


def test_two_concurrent_writers_reserve_exactly_one_row(tmp_path: Path) -> None:
    """The unique index decides, not the order the threads happened to run in."""
    journal = _journal(tmp_path / "home")
    barrier = threading.Barrier(2)
    dispositions: list[str] = []
    identifiers: list[str] = []
    lock = threading.Lock()

    def reserve() -> None:
        barrier.wait(timeout=5)
        reservation = journal.reserve(_spec(), _context())
        with lock:
            dispositions.append(reservation.disposition)
            if reservation.record is not None:
                identifiers.append(reservation.record.operation_id)

    threads = [threading.Thread(target=reserve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(dispositions) == ["existing", "new"]
    assert len(set(identifiers)) == 1, "both writers must agree on one operation"
    assert len(_rows(journal.database_path)) == 1


def test_a_compare_and_set_lets_exactly_one_worker_finish_an_operation(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path / "home")
    reservation = journal.reserve(_spec(), _context())
    assert reservation.record is not None
    operation_id = reservation.record.operation_id

    journal.apply_transition(operation_id, "queued", "running")

    with pytest.raises(IllegalTransition, match="found 'running'"):
        journal.apply_transition(operation_id, "queued", "running")


# -- the state machine -----------------------------------------------------


@pytest.mark.parametrize(
    ("expected", "status"),
    [
        ("queued", "succeeded"),
        ("queued", "outcome_unknown"),
        ("running", "queued"),
        ("succeeded", "running"),
        ("failed", "running"),
        ("outcome_unknown", "succeeded"),
    ],
)
def test_an_illegal_transition_is_refused_before_sqlite_is_touched(
    tmp_path: Path, expected: str, status: str
) -> None:
    journal = _journal(tmp_path / "home")
    reservation = journal.reserve(_spec(), _context())
    assert reservation.record is not None

    with pytest.raises(IllegalTransition, match="not a legal operation transition"):
        journal.apply_transition(
            reservation.record.operation_id,
            expected,  # type: ignore[arg-type]
            status,  # type: ignore[arg-type]
        )


def test_a_success_must_carry_its_receipt_and_a_failure_its_error(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "home")
    reservation = journal.reserve(_spec(), _context())
    assert reservation.record is not None
    operation_id = reservation.record.operation_id
    journal.apply_transition(operation_id, "queued", "running")

    with pytest.raises(OperationError, match="must carry the receipt"):
        journal.apply_transition(operation_id, "running", "succeeded")
    with pytest.raises(OperationError, match="must carry its sanitised error"):
        journal.apply_transition(operation_id, "running", "failed")
    with pytest.raises(OperationError, match="must carry its sanitised error"):
        journal.apply_transition(operation_id, "running", "outcome_unknown")


def test_an_unknown_outcome_is_neither_success_nor_retryable(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "home")
    reservation = journal.reserve(_spec(), _context())
    assert reservation.record is not None
    operation_id = reservation.record.operation_id
    journal.apply_transition(operation_id, "queued", "running")

    record = journal.apply_transition(
        operation_id,
        "running",
        "outcome_unknown",
        None,
        unknown_error("the keys may have reached the pane; no confirmation was observed"),
    )

    assert record.status == "outcome_unknown"
    assert record.receipt is None, "an unknown outcome must not invent a receipt"
    assert record.error is not None
    assert record.error.code == "outcome_unknown"
    assert record.error.retryable is False


def test_a_transition_on_an_unknown_operation_says_so(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "home")

    with pytest.raises(UnknownOperation):
        journal.apply_transition("op_missing", "queued", "running")


def test_every_transition_moves_updated_at(tmp_path: Path) -> None:
    clock = FrozenClock()
    journal = _journal(tmp_path / "home", clock)
    reservation = journal.reserve(_spec(), _context())
    assert reservation.record is not None
    clock.set(NOW + timedelta(seconds=30))

    record = journal.apply_transition(reservation.record.operation_id, "queued", "running")

    assert record.updated_at == NOW + timedelta(seconds=30)
    assert record.submitted_at == NOW


# -- scoped reads ----------------------------------------------------------


def test_another_sessions_operation_is_not_found_rather_than_forbidden(tmp_path: Path) -> None:
    """Not-found on purpose: 'forbidden' would confirm the id exists."""
    journal = _journal(tmp_path / "home")
    reservation = journal.reserve(_spec(), _context(scope="local:session-a"))
    assert reservation.record is not None

    mine = journal.get(reservation.record.operation_id, "local:session-a")
    theirs = journal.get(reservation.record.operation_id, "local:session-b")

    assert mine is not None
    assert mine.operation_id == reservation.record.operation_id
    assert theirs is None


def test_the_wire_operation_carries_no_internal_field(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "home")
    reservation = journal.reserve(_spec(), _context())
    assert reservation.record is not None

    operation = journal.get(reservation.record.operation_id, "local:session-a")

    assert operation is not None
    wire = operation.to_wire()
    assert set(wire) == {
        "operation_id",
        "kind",
        "target",
        "status",
        "submitted_at",
        "updated_at",
        "receipt",
        "error",
    }
    assert "idem-1" not in str(wire), "the client's key is never part of an Operation"
    assert "fingerprint" not in wire
    assert "auth_scope" not in wire


# -- legacy mutations ------------------------------------------------------


def test_a_retained_legacy_mutation_is_journalled(tmp_path: Path) -> None:
    """SHARED.md: no mutation reaches the coordinator without a resolved key."""
    journal = _journal(tmp_path / "home")
    spec = ActionSpec(
        kind="agent.note",
        target=OperationTarget(agent_id="agt-1"),
        body=TellRequest(text="noted"),
    )

    reservation = journal.reserve(spec, _context())

    assert reservation.disposition == "new"
    assert reservation.record is not None
    assert reservation.record.is_wire is False
    assert _rows(journal.database_path)[0]["wire"] == 0


def test_a_legacy_record_refuses_to_become_a_wire_operation(tmp_path: Path) -> None:
    """Otherwise a browser would poll an operation the contract never gave it."""
    journal = _journal(tmp_path / "home")
    spec = ActionSpec(kind="project.freeze", target=OperationTarget(project_id="prj-1"))
    reservation = journal.reserve(spec, _context())
    assert reservation.record is not None

    with pytest.raises(OperationError, match="retained legacy mutation"):
        reservation.record.to_operation()

    assert journal.get(reservation.record.operation_id, "local:session-a") is None


def test_create_or_get_refuses_a_legacy_kind_without_reserving_anything(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "home")
    spec = ActionSpec(kind="task.create", target=OperationTarget(project_id="prj-1"))

    with pytest.raises(OperationError, match="reserve it with reserve"):
        journal.create_or_get(spec, _context())

    assert _rows(journal.database_path) == [], "a refusal must not leave a row behind"


def test_the_two_kind_vocabularies_stay_disjoint_and_complete() -> None:
    assert len(WIRE_OPERATION_KINDS) == 10
    assert len(LEGACY_ACTION_KINDS) == 10
    assert not (WIRE_OPERATION_KINDS & LEGACY_ACTION_KINDS)


# -- restart ---------------------------------------------------------------


def test_a_restart_settles_unfinished_rows_as_unknown_and_repeats_nothing(
    tmp_path: Path,
) -> None:
    clock = FrozenClock()
    journal = _journal(tmp_path / "home", clock)
    queued = journal.reserve(_spec(), _context(key="idem-queued"))
    running = journal.reserve(_spec(agent_id="agt-2"), _context(key="idem-running"))
    assert queued.record is not None
    assert running.record is not None
    journal.apply_transition(running.record.operation_id, "queued", "running")
    clock.set(NOW + timedelta(hours=1))

    settled = journal.reconcile()

    assert {record.status for record in settled} == {"outcome_unknown"}
    assert len(settled) == 2
    for record in settled:
        assert record.reconciled_source == "restart", "the evidence source must be named"
        assert record.reconciled_at == NOW + timedelta(hours=1)
        assert record.receipt is None
    assert journal.unfinished() == ()


def test_a_reconciler_that_can_prove_an_outcome_is_believed(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "home")
    reservation = journal.reserve(_spec(), _context())
    assert reservation.record is not None
    journal.apply_transition(reservation.record.operation_id, "queued", "running")

    def hook(record: OperationRecord) -> Reconciliation:
        return Reconciliation(
            status="succeeded", source="pane observation", receipt=_receipt("observed in the pane")
        )

    settled = journal.reconcile(hook)

    assert [record.status for record in settled] == ["succeeded"]
    assert settled[0].reconciled_source == "pane observation"
    assert settled[0].receipt is not None
    assert settled[0].receipt.detail == "observed in the pane"


def test_reconciliation_leaves_a_settled_row_exactly_as_it_was(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "home")
    reservation = journal.reserve(_spec(), _context())
    assert reservation.record is not None
    operation_id = reservation.record.operation_id
    journal.apply_transition(operation_id, "queued", "running")
    journal.apply_transition(operation_id, "running", "succeeded", _receipt("done once"))

    settled = journal.reconcile()

    assert settled == ()
    after = journal.record(operation_id, "local:session-a")
    assert after is not None
    assert after.status == "succeeded"
    assert after.reconciled_source is None


def test_a_restart_clears_stale_target_locks(tmp_path: Path) -> None:
    """Their holders are gone; a durable row must not block a live office."""
    journal = _journal(tmp_path / "home")
    journal.acquire_lock("agt-1", "op_whatever", "office-dead")

    assert journal.held_targets() == ("agt-1",)

    journal.reconcile()

    assert journal.held_targets() == ()


def test_a_lock_is_released_only_by_the_operation_that_holds_it(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "home")
    journal.acquire_lock("agt-1", "op_one", "office-a")

    journal.release_lock("agt-1", "op_two")
    assert journal.held_targets() == ("agt-1",)

    journal.release_lock("agt-1", "op_one")
    assert journal.held_targets() == ()


# -- bounds and redaction --------------------------------------------------


def test_a_receipt_carrying_a_path_or_a_credential_loses_them(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "home")
    reservation = journal.reserve(_spec(), _context())
    assert reservation.record is not None
    operation_id = reservation.record.operation_id
    journal.apply_transition(operation_id, "queued", "running")

    record = journal.apply_transition(
        operation_id,
        "running",
        "succeeded",
        Receipt(
            delivered="typed",
            detail="wrote /home/someone/.aisquare/context.db using token=sk-livedeadbeef99",
            agent_id="agt-1",
        ),
    )

    stored = str(_rows(journal.database_path)[0]["receipt_json"])
    assert record.receipt is not None
    for secret in ("/home/someone", "context.db", "sk-livedeadbeef99", "token="):
        assert secret not in record.receipt.detail
        assert secret not in stored, "redaction must happen before the row is written"
    assert "<path>" in record.receipt.detail


def test_a_stored_detail_is_redacted_again_when_it_is_read_back(tmp_path: Path) -> None:
    """A row written by an older build must not become a response unchecked."""
    journal = _journal(tmp_path / "home")
    reservation = journal.reserve(_spec(), _context())
    assert reservation.record is not None
    operation_id = reservation.record.operation_id
    connection = sqlite3.connect(str(journal.database_path))
    try:
        connection.execute(
            f"UPDATE {OPERATION_TABLE} SET status = 'succeeded', receipt_json = ? "
            f"WHERE operation_id = ?",
            (
                '{"delivered":"typed","detail":"left in /var/lib/office/state.db"}',
                operation_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    record = journal.record(operation_id, "local:session-a")

    assert record is not None
    assert record.receipt is not None
    assert "/var/lib/office" not in record.receipt.detail


def test_detail_is_bounded_before_it_is_persisted(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "home", limits=OperationLimits(max_detail_chars=40))
    reservation = journal.reserve(_spec(), _context())
    assert reservation.record is not None
    operation_id = reservation.record.operation_id
    journal.apply_transition(operation_id, "queued", "running")

    record = journal.apply_transition(
        operation_id,
        "running",
        "failed",
        None,
        ServiceError(code="internal", detail="x" * 400, retryable=False),
    )

    assert record.error is not None
    assert len(record.error.detail) == 40


def test_persisted_keys_are_bounded(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "home", limits=OperationLimits(max_receipt_keys=2))
    reservation = journal.reserve(_spec(), _context())
    assert reservation.record is not None
    operation_id = reservation.record.operation_id
    journal.apply_transition(operation_id, "queued", "running")

    record = journal.apply_transition(
        operation_id,
        "running",
        "succeeded",
        Receipt(delivered="typed", detail="sent", keys=("a", "b", "c", "d")),
    )

    assert record.receipt is not None
    assert record.receipt.keys == ("a", "b")


# -- retention -------------------------------------------------------------


def test_retention_drops_settled_rows_and_never_an_unknown_one(tmp_path: Path) -> None:
    clock = FrozenClock()
    journal = _journal(tmp_path / "home", clock, OperationLimits(succeeded_retention_s=60.0))
    done = journal.reserve(_spec(), _context(key="idem-done"))
    unsure = journal.reserve(_spec(agent_id="agt-2"), _context(key="idem-unsure"))
    assert done.record is not None
    assert unsure.record is not None
    for record in (done.record, unsure.record):
        journal.apply_transition(record.operation_id, "queued", "running")
    journal.apply_transition(done.record.operation_id, "running", "succeeded", _receipt())
    journal.apply_transition(
        unsure.record.operation_id, "running", "outcome_unknown", None, unknown_error("no proof")
    )

    removed = journal.expire(now=NOW + timedelta(hours=2))

    assert removed == 1
    remaining = [str(row["status"]) for row in _rows(journal.database_path)]
    assert remaining == ["outcome_unknown"], "an unanswered question is not rubbish to collect"


def test_the_row_budget_trims_the_oldest_settled_rows_first(tmp_path: Path) -> None:
    clock = FrozenClock()
    journal = _journal(tmp_path / "home", clock, OperationLimits(max_rows=1))
    for index in range(2):
        clock.set(NOW + timedelta(minutes=index))
        reservation = journal.reserve(_spec(agent_id=f"agt-{index}"), _context(key=f"k-{index}"))
        assert reservation.record is not None
        journal.apply_transition(reservation.record.operation_id, "queued", "running")
        journal.apply_transition(
            reservation.record.operation_id, "running", "succeeded", _receipt()
        )

    removed = journal.expire(now=NOW)

    assert removed == 1
    assert len(_rows(journal.database_path)) == 1


# -- observability ---------------------------------------------------------


def test_an_event_carries_identity_kind_status_and_time_and_nothing_else(
    tmp_path: Path,
) -> None:
    seen: list[OperationEvent] = []
    database = ObservationDatabase.from_config(OfficeConfig(home=tmp_path / "home"), FrozenClock())
    journal = OperationJournal(database, FrozenClock(), listener=seen.append)
    reservation = journal.reserve(_spec(text="a secret instruction"), _context())
    assert reservation.record is not None

    journal.apply_transition(reservation.record.operation_id, "queued", "running")

    assert [event.phase for event in seen] == ["reserved", "transition"]
    assert [event.status for event in seen] == ["queued", "running"]
    assert {event.kind for event in seen} == {"agent.tell"}
    for event in seen:
        assert "secret instruction" not in str(event)
        assert "idem-1" not in str(event)


def test_a_listener_that_raises_cannot_fail_an_operation(tmp_path: Path) -> None:
    """Observability is never allowed to become the thing that breaks a mutation."""

    def explode(event: OperationEvent) -> None:
        raise RuntimeError("the listener is having a day")

    database = ObservationDatabase.from_config(OfficeConfig(home=tmp_path / "home"), FrozenClock())
    journal = OperationJournal(database, FrozenClock(), listener=explode)

    reservation = journal.reserve(_spec(), _context())

    assert reservation.disposition == "new"


# -- the port --------------------------------------------------------------


def test_the_journal_satisfies_the_p01_operation_store_port(tmp_path: Path) -> None:
    """Structural conformance, exercised rather than merely annotated."""
    journal = _journal(tmp_path / "home")
    store: OperationStore = journal

    reservation = store.create_or_get(_spec(), _context())
    assert reservation.operation is not None
    operation = store.transition(reservation.operation.operation_id, "queued", "running")
    fetched = store.get(operation.operation_id, "local:session-a")

    assert reservation.disposition == "new"
    assert operation.status == "running"
    assert fetched is not None
    assert fetched.status == "running"


def test_create_or_get_reports_a_conflict_with_no_operation(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "home")
    journal.create_or_get(_spec(), _context())

    reservation = journal.create_or_get(_spec(text="different"), _context())

    assert reservation.disposition == "conflict"
    assert reservation.operation is None
