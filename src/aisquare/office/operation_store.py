"""The durable operation journal, and migration 002 that gives it tables.

This module is the reliability boundary for every Office mutation. Its one
promise is ordering: **a record is committed before any side effect happens**,
so there is no window in which an agent has been told something and the office
has no idea it happened. Everything else here follows from that promise.

**Uniqueness is ``(auth_scope, idempotency_key)`` and nothing else.** Adding the
kind or the target to the unique index reads like extra safety and is the
opposite: it would let one key be reused for a *different* action and insert
happily alongside the first, which is precisely the reuse that must be refused.
What is compared instead is a canonical FINGERPRINT over kind, resolved target
and validated body. Same fingerprint, same result; different fingerprint, 409
``idempotency_conflict`` with nothing executed.

**``outcome_unknown`` is a first-class answer.** It is not success, not failure,
and never retried automatically. A keystroke that may have landed cannot be
unsent, so the honest record is "we do not know", and a human decides what
happens next with a new key.

**Legacy mutations are journalled but never wire Operations.** ``SHARED.md``
requires that no mutation reaches the coordinator without a resolved internal
key, so the ten changed 1.4 kinds and the ten retained legacy kinds both reserve
here. Only the ten changed kinds can become an
:class:`~aisquare.office.models.Operation` a browser may poll — that separation
is P01's, and :meth:`OperationRecord.to_operation` refuses to blur it.

**Nothing in a row can become an argument.** The auth scope is stored as a
digest, the target holds server-resolved local identifiers only, and receipt and
error text is bounded and redacted twice: once before it is written and once
before it is read back out. No pane id, no tmux target, no path, no credential,
no raw prompt text, no request body.

Connections are short-lived, exactly as the sidecar's own are: one per
operation, WAL, a finite busy timeout, explicit transactions. The journal takes
its path and its busy timeout from the already-constructed
:class:`~aisquare.office.storage.ObservationDatabase` rather than resolving
either itself, so there is one sidecar location in the process and P02 still
owns it.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Literal, cast, get_args

from aisquare.office.models import (
    ActionContext,
    ActionKind,
    ActionSpec,
    LegacyActionKind,
    Operation,
    OperationKind,
    OperationReservation,
    OperationStatus,
    OperationTarget,
    Receipt,
    ServiceError,
    ServiceErrorCode,
)
from aisquare.office.ports import Clock
from aisquare.office.storage import ObservationDatabase, register_migration

OPERATION_TABLE: Final = "office_operation"
"""The journal itself. One row per reserved operation, terminal or not."""

OPERATION_LOCK_TABLE: Final = "office_operation_lock"
"""Which target an executing operation holds. Advisory — see :meth:`acquire_lock`."""

OPERATION_TABLES: Final = (OPERATION_TABLE, OPERATION_LOCK_TABLE)
"""Everything migration 002 creates. None of these is an observation table."""

WIRE_OPERATION_KINDS: Final[frozenset[str]] = frozenset(get_args(OperationKind))
"""Exactly the schema's ten. Derived from the Literal so it cannot drift from it."""

LEGACY_ACTION_KINDS: Final[frozenset[str]] = frozenset(get_args(LegacyActionKind))
"""The retained §4.3 mutations: journalled, never reported as an Operation."""

ACTION_KINDS: Final[frozenset[str]] = WIRE_OPERATION_KINDS | LEGACY_ACTION_KINDS

TERMINAL_STATUSES: Final[frozenset[OperationStatus]] = frozenset(
    {"succeeded", "failed", "outcome_unknown"}
)

#: Which status may follow which. ``queued -> failed`` is here for exactly one
#: case — the worker could not be entered — and ``running -> outcome_unknown``
#: for the case this whole module exists for. Terminal states have no successor;
#: reconciliation is a separate, named operation rather than a hole in this map.
LEGAL_TRANSITIONS: Final[dict[OperationStatus, frozenset[OperationStatus]]] = {
    "queued": frozenset({"running", "failed"}),
    "running": frozenset({"succeeded", "failed", "outcome_unknown"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "outcome_unknown": frozenset(),
}

_PATH_LIKE: Final = re.compile(r"(?<![\w~])(?:~|/)[\w.\-]+(?:/[\w.\-]+)+/?")
"""An absolute or home-relative path anywhere in a diagnostic string."""

_CREDENTIAL_LIKE: Final = re.compile(
    r"(?i)\b(?:api[_-]?key|secret|password|passwd|token|bearer|authorization)"
    r"\s*[:=]\s*\S+|\b(?:sk|pk|ghp|xoxb)-[A-Za-z0-9_-]{8,}"
)
"""``token=abc123``, ``Authorization: Bearer …``, and the common key prefixes."""


class OperationError(RuntimeError):
    """A typed journal failure.

    Typed because the alternative is a ``sqlite3`` exception reaching a response,
    and SQLite's messages carry the database path. The original is kept as
    ``__cause__`` for a local diagnostic and is never part of the message.
    """


class OperationUnavailable(OperationError):
    """The journal could not be written within its short budget.

    The caller answers "unavailable" and executes **nothing**: a mutation whose
    record could not be committed must never run, because a side effect with no
    row is the one failure this module exists to prevent.
    """


class IdempotencyConflict(OperationError):
    """One key, two different actions. Nothing was executed."""

    def __init__(self, idempotency_key: str) -> None:
        super().__init__(
            "this idempotency key was already used for a different kind, target or body"
        )
        self.idempotency_key = idempotency_key


class IllegalTransition(OperationError):
    """A compare-and-set that the state machine, or another worker, refused."""


class UnknownOperation(OperationError):
    """No such operation in this journal."""


@dataclass(frozen=True, slots=True)
class OperationLimits:
    """Every ceiling the journal enforces, as one injectable value.

    Starting points, not measured capacity — the same stance
    :class:`~aisquare.office.storage_schema.StorageLimits` takes next door.
    """

    max_detail_chars: int = 500
    """One bounded sentence of receipt or error detail, after redaction."""

    max_receipt_keys: int = 64
    """Key names kept on a persisted receipt. The wire model bounds this too."""

    max_rows: int = 5_000
    """Row budget, enforced oldest-terminal-first by :meth:`expire`."""

    reservation_attempts: int = 4
    """Retries for a BUSY/LOCKED reservation before answering unavailable."""

    reservation_budget_s: float = 2.0
    """Wall budget for those retries, measured on the monotonic clock."""

    retry_pause_s: float = 0.02
    """Pause between reservation attempts. Short: the busy timeout does the waiting."""

    succeeded_retention_s: float = 7 * 24 * 3600.0
    failed_retention_s: float = 7 * 24 * 3600.0
    """``outcome_unknown`` has no retention: an unknown row survives until it is
    explicitly reconciled, because deleting it would silently convert "we do not
    know" into "it never happened"."""

    def __post_init__(self) -> None:
        for name in ("max_detail_chars", "max_receipt_keys", "max_rows", "reservation_attempts"):
            if getattr(self, name) <= 0:
                raise ValueError(f"OperationLimits.{name} must be positive")
        for name in (
            "reservation_budget_s",
            "retry_pause_s",
            "succeeded_retention_s",
            "failed_retention_s",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"OperationLimits.{name} must be positive")


@dataclass(frozen=True, slots=True)
class OperationEvent:
    """One internal observation of an operation moving.

    Identity, kind, status and time — no request body, no traceback, no
    credential reference. P05 may publish these; nothing in them would be unsafe
    if it did.
    """

    operation_id: str
    kind: ActionKind
    status: OperationStatus
    at: datetime
    phase: Literal["reserved", "transition", "reconciled"]


OperationListener = Callable[[OperationEvent], None]


@dataclass(frozen=True, slots=True)
class OperationRecord:
    """One journal row, as this process works with it.

    Internal: it carries the auth-scope digest, the fingerprint and the
    reconciliation marker, none of which belong on the wire.
    :meth:`to_operation` is the only way out, and it drops all three.
    """

    operation_id: str
    auth_scope_hash: str
    idempotency_key: str
    kind: ActionKind
    target: OperationTarget
    fingerprint: str
    status: OperationStatus
    submitted_at: datetime
    updated_at: datetime
    receipt: Receipt | None = None
    error: ServiceError | None = None
    executor_version: str = ""
    reconciled_source: str | None = None
    reconciled_at: datetime | None = None

    @property
    def is_wire(self) -> bool:
        """Whether this kind is one of the ten a browser may poll."""
        return self.kind in WIRE_OPERATION_KINDS

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def target_key(self) -> str:
        """The serialisation key for this operation's target.

        An operation naming no target serialises against nothing — it gets its
        own key rather than sharing one empty bucket with every other untargeted
        operation, which would queue unrelated work behind unrelated work.
        """
        project, agent = self.target.project_id, self.target.agent_id
        if project is None and agent is None:
            return f"operation:{self.operation_id}"
        return f"{project or ''}\x1f{agent or ''}"

    def to_operation(self) -> Operation:
        """This record as the public ``operation.json`` object.

        Refuses a legacy kind on purpose. ``OperationKind`` is exactly the
        schema's ten, and a retained legacy mutation that appeared here would be
        a browser polling an operation the contract never gave it.
        """
        if not self.is_wire:
            raise OperationError(
                f"{self.kind!r} is a retained legacy mutation and has no wire Operation; "
                "it returns a Receipt or an error"
            )
        return Operation(
            operation_id=self.operation_id,
            kind=_as_operation_kind(self.kind),
            target=self.target,
            status=self.status,
            submitted_at=self.submitted_at,
            updated_at=self.updated_at,
            receipt=self.receipt,
            error=self.error,
        )


@dataclass(frozen=True, slots=True)
class JournalReservation:
    """The internal outcome of reserving, for every kind including legacy ones.

    :class:`~aisquare.office.models.OperationReservation` is its wire-facing
    sibling and can only describe the ten pollable kinds; this one carries the
    record itself, which is what the coordinator actually needs.
    """

    disposition: Literal["new", "existing", "conflict"]
    record: OperationRecord | None = None

    def __post_init__(self) -> None:
        if self.disposition == "conflict":
            if self.record is not None:
                raise ValueError("a conflicting reservation returns no record")
        elif self.record is None:
            raise ValueError(f"a {self.disposition!r} reservation must carry its record")


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """What an executor learned about an operation that outlived its process.

    ``source`` names the evidence — an observed pane, an upstream lookup, the
    restart itself — and is recorded beside the timestamp so a later reader can
    tell a verified outcome from a default one.
    """

    status: OperationStatus
    source: str
    receipt: Receipt | None = None
    error: ServiceError | None = None

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("a reconciliation must name its evidence source")
        if self.status not in TERMINAL_STATUSES:
            raise ValueError("a reconciliation must reach a terminal status")
        if self.status == "succeeded" and self.receipt is None:
            raise ValueError("a reconciled success must carry the receipt it is claiming")
        if self.status != "succeeded" and self.error is None:
            raise ValueError(f"a reconciled {self.status!r} must carry its error")


ReconcileHook = Callable[[OperationRecord], Reconciliation | None]
"""Returns what it can prove, or None to leave the row ``outcome_unknown``."""


def canonical_fingerprint(spec: ActionSpec) -> str:
    """A stable digest of kind, resolved target and validated body.

    Canonical means two things the plan states and one it implies. Object keys
    are sorted and list order is preserved, so the same request hashes the same
    way twice. An absent optional field and an explicit null collapse to one
    representation — ``model_dump`` emits every field, so "not set" and "set to
    None" cannot be told apart and therefore cannot disagree.

    The body's TYPE is part of the digest. Two different request models can have
    identical field values, and hashing only the values would let one key be
    reused across two different actions that happen to look alike.
    """
    body = spec.body
    payload = {
        "kind": spec.kind,
        "target": {
            "project_id": spec.target.project_id,
            "agent_id": spec.target.agent_id,
        },
        "body_type": type(body).__name__ if body is not None else None,
        "body": body.model_dump(mode="json") if body is not None else None,
    }
    text = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class OperationJournal:
    """The journal. Implements P01's :class:`~aisquare.office.ports.OperationStore`.

    Every method is blocking and short; the coordinator runs them on a bounded
    worker. Nothing here knows what an event loop is, and nothing here calls an
    executor — this object records intent and outcome, and the separation is
    what lets a test drive every state without a side effect existing.
    """

    def __init__(
        self,
        database: ObservationDatabase,
        clock: Clock,
        limits: OperationLimits | None = None,
        *,
        listener: OperationListener | None = None,
    ) -> None:
        self._database = database
        self._clock = clock
        self._limits = limits or OperationLimits()
        self._listener = listener
        self._ready = False

    @property
    def limits(self) -> OperationLimits:
        return self._limits

    @property
    def database_path(self) -> Path:
        """The sidecar this journal lives in. P02 resolved it; this never does."""
        return self._database.path

    # -- the P01 port ------------------------------------------------------

    def create_or_get(self, spec: ActionSpec, context: ActionContext) -> OperationReservation:
        """Reserve ``(auth_scope, idempotency_key)``, or report the conflict.

        The wire-facing form of :meth:`reserve`, for the ten pollable kinds. A
        retained legacy mutation is refused here — before it reserves anything —
        because :class:`~aisquare.office.models.OperationReservation` can only
        carry an ``Operation``, and manufacturing one for a legacy note is how a
        browser ends up polling an operation the contract never gave it. The
        coordinator calls :meth:`reserve` and keeps legacy work off the wire.
        """
        if spec.kind not in WIRE_OPERATION_KINDS:
            raise OperationError(
                f"{spec.kind!r} is a retained legacy mutation; reserve it with reserve() — "
                "it has no wire Operation"
            )
        reservation = self.reserve(spec, context)
        if reservation.disposition == "conflict" or reservation.record is None:
            return OperationReservation(disposition="conflict")
        return OperationReservation(
            disposition=reservation.disposition,
            operation=reservation.record.to_operation(),
        )

    def transition(
        self,
        operation_id: str,
        expected: OperationStatus,
        status: OperationStatus,
        receipt: Receipt | None = None,
        error: ServiceError | None = None,
    ) -> Operation:
        """Compare-and-set the status, so two workers cannot both finish one job."""
        return self.apply_transition(operation_id, expected, status, receipt, error).to_operation()

    def get(self, operation_id: str, auth_scope: str) -> Operation | None:
        """Scoped read. Another session's operation is *not found*, not forbidden.

        The distinction is the point: "forbidden" would confirm the id exists,
        which is exactly what a caller guessing ids is trying to learn.
        """
        record = self.record(operation_id, auth_scope)
        if record is None or not record.is_wire:
            return None
        return record.to_operation()

    # -- reservation -------------------------------------------------------

    def reserve(
        self,
        spec: ActionSpec,
        context: ActionContext,
        *,
        executor_version: str = "",
    ) -> JournalReservation:
        """Commit a record, or return the existing one, or report the conflict.

        One short transaction that inserts or reads, compares fingerprints and
        commits. No executor, no network and no tmux call happens while it is
        held, because a transaction spanning a side effect is a lock held for as
        long as the side effect takes.

        The fingerprint is compared BEFORE the status is looked at, so a
        conflicting reuse can never be answered with somebody else's result.
        """
        key = context.idempotency_key
        if not key:
            raise OperationError(
                "a mutation must carry a resolved idempotency key before it reaches the journal"
            )
        if spec.kind not in ACTION_KINDS:
            raise OperationError(f"unknown action kind {spec.kind!r}")

        scope = _scope_digest(context.auth_scope)
        fingerprint = canonical_fingerprint(spec)
        deadline = self._clock.monotonic() + self._limits.reservation_budget_s
        last: sqlite3.OperationalError | None = None

        for attempt in range(self._limits.reservation_attempts):
            try:
                reservation = self._reserve_once(spec, key, scope, fingerprint, executor_version)
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc):
                    raise OperationUnavailable("the journal could not be written") from exc
                last = exc
                if attempt + 1 >= self._limits.reservation_attempts:
                    break
                if self._clock.monotonic() >= deadline:
                    break
                time.sleep(self._limits.retry_pause_s)
                continue
            except sqlite3.Error as exc:
                raise OperationUnavailable("the journal could not be written") from exc
            if reservation.record is not None and reservation.disposition == "new":
                self._emit(reservation.record, "reserved")
            return reservation

        raise OperationUnavailable(
            "the journal stayed busy for the whole reservation budget; the action was not attempted"
        ) from last

    def _reserve_once(
        self,
        spec: ActionSpec,
        key: str,
        scope: str,
        fingerprint: str,
        executor_version: str,
    ) -> JournalReservation:
        now = self._clock.now()
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    f"SELECT * FROM {OPERATION_TABLE} WHERE auth_scope = ? AND idempotency_key = ?",
                    (scope, key),
                ).fetchone()
                if row is None:
                    record = OperationRecord(
                        operation_id=_new_operation_id(),
                        auth_scope_hash=scope,
                        idempotency_key=key,
                        kind=spec.kind,
                        target=spec.target,
                        fingerprint=fingerprint,
                        status="queued",
                        submitted_at=now,
                        updated_at=now,
                        executor_version=executor_version,
                    )
                    self._insert(connection, record)
                    connection.execute("COMMIT")
                    return JournalReservation(disposition="new", record=record)
                existing = self._record_from_row(row)
                connection.execute("COMMIT")
            except sqlite3.Error:
                with suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise
        if existing.fingerprint != fingerprint:
            return JournalReservation(disposition="conflict")
        return JournalReservation(disposition="existing", record=existing)

    # -- transitions -------------------------------------------------------

    def apply_transition(
        self,
        operation_id: str,
        expected: OperationStatus,
        status: OperationStatus,
        receipt: Receipt | None = None,
        error: ServiceError | None = None,
    ) -> OperationRecord:
        """The compare-and-set, returning the internal record.

        Two guards, in this order. The state machine refuses a move it does not
        define, so an illegal transition never reaches SQLite. Then the UPDATE
        carries ``WHERE status = expected``, so of two workers that both believe
        an operation is theirs to finish, exactly one writes and the other is
        told which status it actually found.
        """
        if status not in LEGAL_TRANSITIONS[expected]:
            raise IllegalTransition(
                f"{expected!r} -> {status!r} is not a legal operation transition"
            )
        if status == "succeeded" and receipt is None:
            raise OperationError("a succeeded operation must carry the receipt it is claiming")
        if status != "succeeded" and receipt is not None:
            raise OperationError(f"a {status!r} operation carries no receipt")
        if status in ("failed", "outcome_unknown") and error is None:
            raise OperationError(f"a {status!r} operation must carry its sanitised error")
        if status not in ("failed", "outcome_unknown") and error is not None:
            raise OperationError(f"a {status!r} operation carries no error")

        bounded_receipt = self._bound_receipt(receipt)
        bounded_error = self._bound_error(error)
        now = self._clock.now()
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    f"UPDATE {OPERATION_TABLE} SET status = ?, updated_at = ?, "
                    f"receipt_json = ?, error_json = ? WHERE operation_id = ? AND status = ?",
                    (
                        status,
                        _iso(now),
                        _encode(bounded_receipt),
                        _encode(bounded_error),
                        operation_id,
                        expected,
                    ),
                )
                changed = cursor.rowcount
                row = connection.execute(
                    f"SELECT * FROM {OPERATION_TABLE} WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except sqlite3.Error as exc:
                with suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise OperationUnavailable("the journal could not be updated") from exc

        if row is None:
            raise UnknownOperation(f"no operation {operation_id!r} in this journal")
        record = self._record_from_row(row)
        if changed == 0:
            raise IllegalTransition(
                f"operation {operation_id!r} expected status {expected!r} "
                f"but found {record.status!r}"
            )
        self._emit(record, "transition")
        return record

    # -- reads -------------------------------------------------------------

    def record(self, operation_id: str, auth_scope: str) -> OperationRecord | None:
        """One record within its own scope, or None."""
        scope = _scope_digest(auth_scope)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT * FROM {OPERATION_TABLE} WHERE operation_id = ? AND auth_scope = ?",
                (operation_id, scope),
            ).fetchone()
        return None if row is None else self._record_from_row(row)

    def unfinished(self) -> tuple[OperationRecord, ...]:
        """Every record that is still ``queued`` or ``running``, oldest first."""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM {OPERATION_TABLE} WHERE status IN ('queued', 'running') "
                f"ORDER BY submitted_at, operation_id"
            ).fetchall()
        return tuple(self._record_from_row(row) for row in rows)

    def held_targets(self) -> tuple[str, ...]:
        """Target keys currently recorded as held, for diagnosis and tests."""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT target_key FROM {OPERATION_LOCK_TABLE} ORDER BY target_key"
            ).fetchall()
        return tuple(str(row["target_key"]) for row in rows)

    # -- locks -------------------------------------------------------------

    def acquire_lock(self, target_key: str, operation_id: str, holder: str) -> None:
        """Record that ``operation_id`` holds ``target_key``.

        Advisory, and deliberately so. Serialisation is enforced in the process
        by the coordinator's own locks — the plan allows exactly one Office
        server per resolved home — and a durable row that OUTLIVED its holder
        would block every later action on that target with no way to tell a live
        holder from a dead one. What the row buys is a restart being able to say
        which target an unfinished operation was touching.
        """
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    f"INSERT OR REPLACE INTO {OPERATION_LOCK_TABLE}"
                    f"(target_key, operation_id, holder, acquired_at) VALUES (?, ?, ?, ?)",
                    (target_key, operation_id, holder, _iso(self._clock.now())),
                )
                connection.execute("COMMIT")
            except sqlite3.Error as exc:
                with suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise OperationUnavailable("the journal could not record a target lock") from exc

    def release_lock(self, target_key: str, operation_id: str) -> None:
        """Drop the row, but only if this operation still owns it."""
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    f"DELETE FROM {OPERATION_LOCK_TABLE} WHERE target_key = ? AND operation_id = ?",
                    (target_key, operation_id),
                )
                connection.execute("COMMIT")
            except sqlite3.Error as exc:
                with suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise OperationUnavailable("the journal could not release a target lock") from exc

    # -- restart -----------------------------------------------------------

    def reconcile(self, hook: ReconcileHook | None = None) -> tuple[OperationRecord, ...]:
        """Settle every unfinished row, without repeating a single side effect.

        A row that was ``running`` when the process died is not evidence that the
        action failed, and it is not permission to do it again. Each one is
        offered to the executor-supplied ``hook``, which returns what it can
        actually prove; anything it cannot prove becomes ``outcome_unknown``
        with the restart named as the source. Nothing is re-executed here, ever.

        Stale lock rows are dropped at the same time: their holders are gone.
        """
        now = self._clock.now()
        settled: list[OperationRecord] = []
        for record in self.unfinished():
            outcome = hook(record) if hook is not None else None
            if outcome is None:
                outcome = Reconciliation(
                    status="outcome_unknown",
                    source="restart",
                    error=ServiceError(
                        code="outcome_unknown",
                        detail=(
                            "this operation was still unfinished when the office restarted; "
                            "whether it reached the agent was not observed and it was not repeated"
                        ),
                        retryable=False,
                    ),
                )
            settled.append(self._settle(record, outcome, now))
        self._clear_locks()
        return tuple(settled)

    def _settle(
        self, record: OperationRecord, outcome: Reconciliation, now: datetime
    ) -> OperationRecord:
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    f"UPDATE {OPERATION_TABLE} SET status = ?, updated_at = ?, receipt_json = ?, "
                    f"error_json = ?, reconciled_source = ?, reconciled_at = ? "
                    f"WHERE operation_id = ? AND status IN ('queued', 'running')",
                    (
                        outcome.status,
                        _iso(now),
                        _encode(self._bound_receipt(outcome.receipt)),
                        _encode(self._bound_error(outcome.error)),
                        outcome.source[: self._limits.max_detail_chars],
                        _iso(now),
                        record.operation_id,
                    ),
                )
                row = connection.execute(
                    f"SELECT * FROM {OPERATION_TABLE} WHERE operation_id = ?",
                    (record.operation_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except sqlite3.Error as exc:
                with suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise OperationUnavailable("the journal could not be reconciled") from exc
        settled = self._record_from_row(row)
        self._emit(settled, "reconciled")
        return settled

    def _clear_locks(self) -> None:
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(f"DELETE FROM {OPERATION_LOCK_TABLE}")
                connection.execute("COMMIT")
            except sqlite3.Error as exc:
                with suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise OperationUnavailable("stale target locks could not be cleared") from exc

    # -- retention ---------------------------------------------------------

    def expire(self, *, now: datetime) -> int:
        """Delete settled rows past their window, and trim to the row budget.

        ``outcome_unknown`` is never deleted by either rule. An unknown row is
        the record of a question nobody answered, and expiring it would turn
        that question into a silent "it never happened".
        """
        succeeded_cutoff = _iso(now - timedelta(seconds=self._limits.succeeded_retention_s))
        failed_cutoff = _iso(now - timedelta(seconds=self._limits.failed_retention_s))
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                removed = connection.execute(
                    f"DELETE FROM {OPERATION_TABLE} WHERE "
                    f"(status = 'succeeded' AND updated_at < ?) OR "
                    f"(status = 'failed' AND updated_at < ?)",
                    (succeeded_cutoff, failed_cutoff),
                ).rowcount
                row = connection.execute(f"SELECT COUNT(*) AS n FROM {OPERATION_TABLE}").fetchone()
                over = int(row["n"]) - self._limits.max_rows
                if over > 0:
                    removed += connection.execute(
                        f"DELETE FROM {OPERATION_TABLE} WHERE operation_id IN ("
                        f"  SELECT operation_id FROM {OPERATION_TABLE} "
                        f"  WHERE status IN ('succeeded', 'failed') "
                        f"  ORDER BY updated_at LIMIT ?"
                        f")",
                        (over,),
                    ).rowcount
                connection.execute("COMMIT")
            except sqlite3.Error as exc:
                with suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise OperationUnavailable("the journal could not be trimmed") from exc
        return removed

    # -- internals ---------------------------------------------------------

    def _emit(
        self, record: OperationRecord, phase: Literal["reserved", "transition", "reconciled"]
    ) -> None:
        """Hand one bounded event to the listener; never fail an operation for it."""
        if self._listener is None:
            return
        event = OperationEvent(
            operation_id=record.operation_id,
            kind=record.kind,
            status=record.status,
            at=record.updated_at,
            phase=phase,
        )
        with suppress(Exception):
            self._listener(event)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """One short-lived connection to the sidecar P02 already resolved.

        ``isolation_level=None`` because every transaction here is explicit: the
        driver's implicit BEGIN would wrap statements this code has already
        chosen the boundaries of, and a compare-and-set whose read and write are
        in different transactions is not a compare-and-set.
        """
        if not self._ready:
            self._database.migrate()
            self._ready = True
        timeout_ms = self._database.limits.busy_timeout_ms
        try:
            connection = sqlite3.connect(
                str(self._database.path),
                timeout=timeout_ms / 1000,
                isolation_level=None,
            )
        except sqlite3.Error as exc:
            raise OperationUnavailable("the journal could not be opened") from exc
        connection.row_factory = sqlite3.Row
        try:
            connection.execute(f"PRAGMA busy_timeout = {timeout_ms}")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = NORMAL")
            yield connection
        finally:
            with suppress(sqlite3.Error):
                connection.close()

    def _insert(self, connection: sqlite3.Connection, record: OperationRecord) -> None:
        connection.execute(
            f"INSERT INTO {OPERATION_TABLE}("
            f"operation_id, auth_scope, idempotency_key, kind, wire, project_id, agent_id, "
            f"fingerprint, status, submitted_at, updated_at, receipt_json, error_json, "
            f"executor_version, reconciled_source, reconciled_at) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.operation_id,
                record.auth_scope_hash,
                record.idempotency_key,
                record.kind,
                1 if record.is_wire else 0,
                record.target.project_id,
                record.target.agent_id,
                record.fingerprint,
                record.status,
                _iso(record.submitted_at),
                _iso(record.updated_at),
                _encode(self._bound_receipt(record.receipt)),
                _encode(self._bound_error(record.error)),
                record.executor_version[: self._limits.max_detail_chars],
                record.reconciled_source,
                _iso(record.reconciled_at) if record.reconciled_at is not None else None,
            ),
        )

    def _record_from_row(self, row: sqlite3.Row) -> OperationRecord:
        receipt_json = row["receipt_json"]
        error_json = row["error_json"]
        return OperationRecord(
            operation_id=str(row["operation_id"]),
            auth_scope_hash=str(row["auth_scope"]),
            idempotency_key=str(row["idempotency_key"]),
            kind=_as_action_kind(str(row["kind"])),
            target=OperationTarget(
                project_id=row["project_id"],
                agent_id=row["agent_id"],
            ),
            fingerprint=str(row["fingerprint"]),
            status=_as_status(str(row["status"])),
            submitted_at=_parse(str(row["submitted_at"])),
            updated_at=_parse(str(row["updated_at"])),
            receipt=self._bound_receipt(
                Receipt.model_validate(json.loads(receipt_json)) if receipt_json else None
            ),
            error=self._bound_error(
                ServiceError.model_validate(json.loads(error_json)) if error_json else None
            ),
            executor_version=str(row["executor_version"] or ""),
            reconciled_source=(
                str(row["reconciled_source"]) if row["reconciled_source"] is not None else None
            ),
            reconciled_at=(
                _parse(str(row["reconciled_at"])) if row["reconciled_at"] is not None else None
            ),
        )

    def _bound_receipt(self, receipt: Receipt | None) -> Receipt | None:
        """Redact and bound a receipt, on the way in AND on the way out.

        Twice on purpose. A row written by an older build, or by a caller that
        learned a new way to embed a path, must not become a response simply
        because it is already stored.
        """
        if receipt is None:
            return None
        keys = receipt.keys
        return Receipt(
            delivered=receipt.delivered,
            detail=self._bound_text(receipt.detail),
            agent_id=receipt.agent_id,
            keys=None if keys is None else keys[: self._limits.max_receipt_keys],
            at=receipt.at,
            closed=receipt.closed,
        )

    def _bound_error(self, error: ServiceError | None) -> ServiceError | None:
        if error is None:
            return None
        return ServiceError(
            code=error.code,
            detail=self._bound_text(error.detail),
            retryable=error.retryable,
        )

    def _bound_text(self, text: str) -> str:
        """One sanitised, bounded sentence: no path, no credential, no novel."""
        cleaned = _CREDENTIAL_LIKE.sub("<redacted>", text)
        cleaned = _PATH_LIKE.sub("<path>", cleaned)
        cleaned = cleaned.strip()
        if len(cleaned) > self._limits.max_detail_chars:
            cleaned = cleaned[: self._limits.max_detail_chars - 1].rstrip() + "…"
        return cleaned or "<redacted>"


# --------------------------------------------------------------------------
# migration 002
# --------------------------------------------------------------------------

#: Migration 002. Operations, their idempotency reservation, and the advisory
#: target-lock rows.
#:
#: The unique index is over ``(auth_scope, idempotency_key)`` ONLY. Widening it
#: with the kind or the target would let the same key be reused for a different
#: action and inserted beside the first, which is the exact reuse that must be
#: refused — the fingerprint column is what detects that, not the index.
_MIGRATION_002: Final[Sequence[str]] = (
    f"""
    CREATE TABLE {OPERATION_TABLE} (
        operation_id      TEXT PRIMARY KEY,
        auth_scope        TEXT NOT NULL,
        idempotency_key   TEXT NOT NULL,
        kind              TEXT NOT NULL,
        wire              INTEGER NOT NULL,
        project_id        TEXT,
        agent_id          TEXT,
        fingerprint       TEXT NOT NULL,
        status            TEXT NOT NULL,
        submitted_at      TEXT NOT NULL,
        updated_at        TEXT NOT NULL,
        receipt_json      TEXT,
        error_json        TEXT,
        executor_version  TEXT NOT NULL DEFAULT '',
        reconciled_source TEXT,
        reconciled_at     TEXT
    )
    """,
    f"""
    CREATE UNIQUE INDEX ux_operation_key
        ON {OPERATION_TABLE}(auth_scope, idempotency_key)
    """,
    f"CREATE INDEX ix_operation_status ON {OPERATION_TABLE}(status, updated_at)",
    f"CREATE INDEX ix_operation_scope ON {OPERATION_TABLE}(auth_scope, submitted_at)",
    # One row per held target, deleted on release and cleared at startup. Not a
    # mutex: the holder may be a process that no longer exists, and a durable
    # row that outlived it must never be able to block a live office.
    f"""
    CREATE TABLE {OPERATION_LOCK_TABLE} (
        target_key   TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL,
        holder       TEXT NOT NULL,
        acquired_at  TEXT NOT NULL
    )
    """,
)


def apply_migration_002(connection: sqlite3.Connection) -> None:
    """Create the operation tables inside the transaction the sidecar opened.

    Statements run one at a time rather than through ``executescript``: that
    method issues an implicit COMMIT before its script, which would release the
    caller's transaction and split this schema change from the version row that
    records it. P02's migration 001 records the same lesson at length.
    """
    for statement in _MIGRATION_002:
        connection.execute(statement)


MIGRATION_002: Final = register_migration(2, "operations", apply_migration_002)
"""Registered at import, through P02's seam and nothing else.

Registration-time validation is the point: a duplicate or out-of-order version
is a packaging mistake, and the useful moment to refuse it is when this module
is imported, not on the first open on somebody's machine.
"""


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _new_operation_id() -> str:
    """Opaque, server-generated, and never the caller's idempotency key.

    ``uuid4`` rather than ``secrets``: the id is an unguessable handle inside an
    already-authenticated scope, and importing ``secrets`` at module scope drags
    ``hmac`` and ``random`` onto the CLI's hook path — a cost
    ``tests/test_iam_single_reader.py`` deliberately pins out.
    """
    return f"op_{uuid.uuid4().hex}"


def _scope_digest(auth_scope: str) -> str:
    """The local auth-session scope as a digest, never in the clear."""
    return hashlib.sha256(auth_scope.encode("utf-8")).hexdigest()


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _encode(model: Receipt | ServiceError | None) -> str | None:
    if model is None:
        return None
    return json.dumps(model.to_wire(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _as_status(value: str) -> OperationStatus:
    if value not in LEGAL_TRANSITIONS:
        raise OperationError("the journal holds an operation status this build does not know")
    return value


def _as_action_kind(value: str) -> ActionKind:
    if value not in ACTION_KINDS:
        raise OperationError("the journal holds an action kind this build does not know")
    return cast(ActionKind, value)


def _as_operation_kind(value: ActionKind) -> OperationKind:
    if value not in WIRE_OPERATION_KINDS:  # pragma: no cover - to_operation checks first
        raise OperationError(f"{value!r} is not a wire operation kind")
    return cast(OperationKind, value)


def unknown_error(detail: str) -> ServiceError:
    """The one shape an uncertain outcome may take: never retryable.

    ``retryable`` is False and cannot be otherwise. An uncertain mutation is
    exactly the case where a retry might do the thing twice, so the flag that
    invites a client to repeat it is the flag that must stay down.
    """
    return ServiceError(code="outcome_unknown", detail=detail, retryable=False)


def failure_error(
    detail: str, *, code: ServiceErrorCode = "internal", retryable: bool = False
) -> ServiceError:
    """A sanitised failure. The executor classifies; the journal only bounds."""
    return ServiceError(code=code, detail=detail, retryable=retryable)


__all__ = [
    "ACTION_KINDS",
    "LEGACY_ACTION_KINDS",
    "LEGAL_TRANSITIONS",
    "MIGRATION_002",
    "OPERATION_LOCK_TABLE",
    "OPERATION_TABLE",
    "OPERATION_TABLES",
    "TERMINAL_STATUSES",
    "WIRE_OPERATION_KINDS",
    "IdempotencyConflict",
    "IllegalTransition",
    "JournalReservation",
    "OperationError",
    "OperationEvent",
    "OperationJournal",
    "OperationLimits",
    "OperationListener",
    "OperationRecord",
    "OperationUnavailable",
    "ReconcileHook",
    "Reconciliation",
    "UnknownOperation",
    "apply_migration_002",
    "canonical_fingerprint",
    "failure_error",
    "unknown_error",
]
