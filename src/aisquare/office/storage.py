"""The Office sidecar: bounded latest facts, correlations and source cursors.

A cache of evidence. The CLI's ``context.db`` stays authoritative for projects,
sessions, tasks, fleet rows and events; nothing here writes a ``TeamEvent``,
and nothing here is consulted when the CLI can answer. What this store exists
for is the part the CLI does not keep: what a pane looked like at the last
poll, which prompt lifecycle an agent is in, what a hook reported, how far an
incremental consumer has read, and whether a local session was ever provably
joined to a deployed run.

Four properties are load-bearing, and each has a test with its name on it.

**Independent facts, not whole rows.** ``merge`` upserts fact by fact under one
transaction. Hooks and the poller write concurrently: a merge that replaced
rows would let a hook that knows one column erase the columns the poll had just
learned.

**Generation before time.** An older observation cannot overwrite a newer one,
whichever writer arrives last. Generation decides; equal generations fall back
to ``observed_at``; a genuine tie falls back to a fixed source ranking, so two
writers racing on identical evidence converge instead of flapping.

**One transaction for a cursor and its rows.** ``write_checkpoint`` moves a
cursor and commits the facts derived from that move together, under a
compare-and-set on the generation. A cursor that advanced without its rows lost
the work; rows without the cursor are counted twice on the next pass.

**Nothing here becomes an argument.** The sidecar stores a project's *label*,
never its root; a session's identity, never its transcript path; a bounded,
redacted pane tail, never scrollback; a binding's id and revision, never the
workspace key. Failures are :class:`StorageError` subclasses whose messages
carry no path and no SQLite text.

Connections are short-lived by design: one per operation, WAL, a finite busy
timeout, explicit transactions. The alternative — a long-lived connection owned
by this object — is a connection crossing the thread boundary that P05's worker
and P03's collection already cross.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from aisquare.models import TeamSession
from aisquare.office.config import OfficeConfig
from aisquare.office.models import (
    AskQuestion,
    CheckpointConsumer,
    CheckpointRecord,
    DetectedBy,
    HookFact,
    LocalObservationBatch,
    ObservationCategory,
    ObservationUpdate,
    PaneHealth,
    PaneObservation,
    PromptEvidence,
    QuestionKind,
    QuestionOption,
    TerminalPaneFacts,
)
from aisquare.office.ports import Clock
from aisquare.office.storage_schema import (
    MIGRATIONS,
    OBSERVATION_TABLES,
    SCHEMA_TABLE,
    MigrationRegistry,
    StorageBoundsError,
    StorageError,
    StorageLimits,
    StorageMigrationError,
    StoragePathError,
    StorageUnavailable,
    applied_version,
    ensure_schema_table,
    register_migration,
)

CHECKPOINT_CONSUMERS: Final[frozenset[str]] = frozenset({"activity", "cost"})
"""P01's :data:`CheckpointConsumer` as a runtime set. Anything else is refused —
a typo in a namespace is a cursor nobody ever reads and work redone forever."""

OBSERVATION_CATEGORIES: Final[frozenset[str]] = frozenset(
    {"project", "session", "task", "fleet", "pane", "prompt", "cost", "activity"}
)
"""P01's :data:`ObservationCategory`, checked at the boundary for the same reason."""

CorrelationStatus = Literal["verified", "unjoined", "stale"]

#: How a genuine tie is broken: same generation, same instant, two sources.
#: A hook saw the event itself; a poll inferred it from a pane afterwards, so
#: the hook is the better evidence. Fixed and total so two racing writers reach
#: the same answer rather than overwriting each other in a loop.
_SOURCE_RANK: Final[Mapping[str, int]] = {"hook": 0, "poll": 1, "derived": 2}
_UNRANKED: Final = 9

DEFAULT_SOURCE: Final = "poll"

_PATH_LIKE: Final = re.compile(r"(?<![\w~])(?:~|/)[\w.\-]+(?:/[\w.\-]+)+/?")
"""Two-segment-or-deeper absolute or home-relative paths, for redaction.

Deliberately not "anything with a slash": ``and/or`` and ``n/a`` appear in
ordinary prompt text and are not paths. What this catches is the shape that
leaks a home directory layout into stored evidence.
"""

_CONTEXT_DB_NAME: Final = "context.db"

BOARD_SEQ_KEY: Final = "board:seq"
"""The reserved observation key the board sequence is kept under.

A fact rather than a column of a project row: a batch collected before any
project has been observed still knows the sequence, and it has to survive a
restart — a reconnecting client told the board rewound to zero re-renders
everything and reports every agent as new.
"""


def _source_rank(source: object) -> int:
    """Rank for the tie-break, exposed to SQL as ``source_rank()``."""
    return _SOURCE_RANK.get(str(source), _UNRANKED)


def _iso(value: datetime) -> str:
    """A sortable UTC timestamp.

    Always ``+00:00`` and always the same width, so SQLite's lexicographic
    comparison on the TEXT column is a chronological comparison. A naive
    datetime is refused rather than assumed to be local time: every freshness
    and retention decision below is arithmetic on these strings.
    """
    if value.tzinfo is None:
        raise StorageError("timestamps must be timezone-aware; a naive datetime has no instant")
    return value.astimezone(UTC).isoformat()


def _parse(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:  # pragma: no cover - only a hand-edited database
        raise StorageUnavailable("a stored timestamp is not readable") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _require(value: str | None, what: str) -> datetime:
    parsed = _parse(value)
    if parsed is None:  # pragma: no cover - NOT NULL columns
        raise StorageUnavailable(f"{what} is missing from a stored row")
    return parsed


@dataclass(frozen=True, slots=True)
class ProjectObservation:
    """The bounded project facts the sidecar keeps — a label, never a root.

    Not a :class:`aisquare.models.ProjectInfo`, and that is the whole point.
    ``ProjectInfo.root`` is a real filesystem path; this table deliberately
    never stores one, so reconstructing that model would mean inventing the
    field it exists to withhold. P04 reads project identity from the live CLI
    store, which owns it; this record is freshness evidence.
    """

    project_id: str
    name: str
    root_label: str
    codename: str | None = None
    frozen: bool | None = None
    board_seq: int | None = None
    observed_at: datetime | None = None
    expires_at: datetime | None = None
    source: str = DEFAULT_SOURCE
    generation: int = 0


@dataclass(frozen=True, slots=True)
class CorrelationRecord:
    """One local session's relationship to one deployed run, with its evidence.

    Internal: P13 reads it, no browser ever sees it. ``correlation_id`` is
    derived from the join tuple when absent, which makes an upsert idempotent
    without the caller having to mint and remember an id.

    ``status`` distinguishes the three states P13 must be able to tell apart
    after a restart: ``verified`` (a join was proven), ``unjoined`` (looked,
    found nothing — which is an answer, not an outage) and ``stale`` (it was
    verified once, against evidence now too old to assert).
    """

    project_id: str
    binding_id: str
    binding_revision: int
    pipeline_marker: str
    observed_at: datetime
    status: CorrelationStatus = "unjoined"
    session_id: str | None = None
    run_id: str | None = None
    join_evidence: Mapping[str, str | int | float | bool | None] = field(default_factory=dict)
    last_verified_at: datetime | None = None
    expires_at: datetime | None = None
    source: str = DEFAULT_SOURCE
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        if not self.project_id:
            raise ValueError("CorrelationRecord.project_id must not be empty")
        if not self.binding_id:
            raise ValueError("CorrelationRecord.binding_id must not be empty")
        if self.status == "verified" and self.run_id is None:
            raise ValueError("a verified correlation names the run it verified")

    @property
    def join_key(self) -> tuple[str, str, str, int, str]:
        """The tuple uniqueness is defined over."""
        return (
            self.project_id,
            self.session_id or "",
            self.binding_id,
            self.binding_revision,
            self.run_id or "",
        )

    def resolved_id(self) -> str:
        """``correlation_id`` if given, else one derived from :attr:`join_key`."""
        if self.correlation_id:
            return self.correlation_id
        digest = hashlib.sha256("\x1f".join(str(part) for part in self.join_key).encode())
        return digest.hexdigest()[:32]


class ObservationDatabase:
    """The sidecar. Implements P01's :class:`~aisquare.office.ports.ObservationStore`.

    Every method is blocking and short. The caller — P05's worker, P03's
    collection — runs them off the event loop; nothing in this class knows what
    an event loop is.
    """

    def __init__(
        self,
        path: Path,
        clock: Clock,
        limits: StorageLimits | None = None,
        *,
        home: Path | None = None,
        registry: MigrationRegistry | None = None,
    ) -> None:
        self._limits = limits or StorageLimits()
        self._clock = clock
        self._registry = registry if registry is not None else MIGRATIONS
        self._path = _validated_path(path, home)
        self._lock = threading.Lock()
        self._ready = False
        self._closed = False

    @classmethod
    def from_config(
        cls,
        config: OfficeConfig,
        clock: Clock,
        limits: StorageLimits | None = None,
        *,
        registry: MigrationRegistry | None = None,
    ) -> ObservationDatabase:
        """Build from :attr:`OfficeConfig.sidecar_path`, bounded by the same home.

        The home is passed as the boundary rather than trusted implicitly: the
        config computes the path, and this check proves the computed path did
        not escape the home it was computed from.
        """
        return cls(
            config.sidecar_path,
            clock,
            limits,
            home=config.home,
            registry=registry,
        )

    @property
    def path(self) -> Path:
        return self._path

    @property
    def limits(self) -> StorageLimits:
        return self._limits

    # -- lifecycle ---------------------------------------------------------

    def migrate(self) -> int:
        """Apply every pending migration; return the schema version after.

        Each migration runs inside its own ``BEGIN IMMEDIATE`` together with
        the row that records it, so a failed callback rolls back its own
        transaction and leaves the previous version readable and usable. The
        write lock taken by ``IMMEDIATE`` is what makes the schema change
        single-writer: a second process opening the same sidecar waits on the
        busy timeout and then finds the work already done.
        """
        self._check_open()
        with self._lock, self._connect(migrating=True) as connection:
            ensure_schema_table(connection)
            current = applied_version(connection)
            latest = self._registry.latest_version()
            if current > latest:
                raise StorageUnavailable(
                    f"the sidecar is at schema version {current}, newer than this build "
                    f"understands ({latest}); it is not readable here"
                )
            for migration in self._registry.pending(current):
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    if applied_version(connection) >= migration.version:
                        connection.execute("COMMIT")
                        continue
                    migration.apply(connection)
                    connection.execute(
                        f"INSERT INTO {SCHEMA_TABLE}(version, name, applied_at) VALUES (?, ?, ?)",
                        (migration.version, migration.name, _iso(self._clock.now())),
                    )
                    connection.execute("COMMIT")
                except Exception as exc:
                    with suppress(sqlite3.Error):
                        connection.execute("ROLLBACK")
                    raise StorageMigrationError(
                        f"migration {migration.version} ({migration.name!r}) failed and was "
                        f"rolled back; the sidecar is still at version "
                        f"{applied_version(connection)}"
                    ) from exc
            version = applied_version(connection)
        self._ready = True
        return version

    def schema_version(self) -> int:
        """The applied version, without migrating. 0 on an untouched database."""
        self._check_open()
        with self._connect() as connection:
            ensure_schema_table(connection)
            return applied_version(connection)

    def close(self) -> None:
        """Release the sidecar. Idempotent.

        Connections are per-operation, so there is nothing to close but the
        WAL, and that is checkpointed with ``TRUNCATE`` on a best-effort basis:
        a sidecar left with a large WAL after a busy session is a file that
        looks corrupt to an operator and is merely unflushed. Best effort
        because a checkpoint blocked by another reader is not a reason to fail
        a shutdown.
        """
        if self._closed:
            return
        self._closed = True
        if self._path.exists():
            with (
                suppress(sqlite3.Error, OSError),
                self._connect(check_closed=False) as connection,
            ):
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # -- the observation port ---------------------------------------------

    def read(self, *, now: datetime) -> LocalObservationBatch:
        """The latest unexpired facts, as one immutable batch.

        ``projects`` is empty and ``partial`` is True, both deliberately. The
        sidecar keeps a project *label*, never a root, so it cannot honestly
        build a :class:`~aisquare.models.ProjectInfo`; and it keeps no tasks,
        events, fleet rows or turns at all, because the CLI store owns those.
        Read :meth:`project_observations` for what it does have. A consumer
        that treated this batch as a complete collection would be reading a
        cache as if it were the board.
        """
        self._ensure_ready()
        cutoff = _iso(now)
        with self._connect() as connection:
            sessions = self._read_sessions(connection, cutoff)
            panes = self._read_panes(connection, cutoff)
            prompts = self._read_prompts(connection, cutoff)
            hooks = self._read_hooks(connection, cutoff)
            row = connection.execute(
                "SELECT generation FROM office_observation_meta WHERE key = ?",
                (BOARD_SEQ_KEY,),
            ).fetchone()
        return LocalObservationBatch(
            collected_at=now,
            board_seq=int(row["generation"]) if row is not None else 0,
            sessions=sessions,
            panes=panes,
            prompts=prompts,
            hook_facts=hooks,
            partial=True,
        )

    def merge(self, batch: LocalObservationBatch) -> None:
        """Upsert every fact in the batch, generation-ordered, in one transaction.

        All of it or none of it: a bounds violation anywhere — one oversized
        pane tail, one unencodable fact — rolls the whole batch back rather
        than leaving a snapshot half-applied, because a half-applied batch is
        indistinguishable from a real observation of an inconsistent machine.
        """
        self._ensure_ready()
        observed = batch.collected_at
        with self._lock, self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                for project in batch.projects:
                    self._merge_project(connection, project, batch.board_seq, observed)
                for session in batch.sessions:
                    self._merge_session(connection, session, batch.board_seq, observed)
                for pane in batch.panes:
                    self._merge_pane(connection, pane, batch.board_seq)
                for prompt in batch.prompts:
                    self._merge_prompt(connection, prompt)
                for fact in batch.hook_facts:
                    self._merge_hook(connection, fact, batch.board_seq)
                # The board sequence is a fact in its own right, not a column of
                # a project row: a batch collected before any project was
                # observed still knows it, and it has to survive a restart so a
                # reconnecting client is not told the board rewound to zero.
                # Stored with the sequence AS the generation, so a poll that
                # completes out of order cannot lower it.
                self._apply_update(
                    connection,
                    ObservationUpdate(
                        category="project",
                        key=BOARD_SEQ_KEY,
                        payload={"seq": int(batch.board_seq)},
                        generation=int(batch.board_seq),
                        observed_at=observed,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                with suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise

    def expire(self, *, now: datetime) -> int:
        """Delete what policy says is past, and only that. Return the count.

        Three passes, in order: rows whose own ``expires_at`` has arrived, rows
        past their category's retention window, then — only if the file is over
        the byte budget — the oldest rows in the largest tables.

        A LIVE session is never aged out. Retention measures an *ended*
        session's row from when it ended, so an agent that has been running for
        a fortnight keeps its fact, and a source that went unreachable deletes
        nothing at all: absence of a new batch is not evidence that anything
        stopped.
        """
        self._ensure_ready()
        stamp = _iso(now)
        deleted = 0
        with self._lock, self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                for table in OBSERVATION_TABLES:
                    cursor = connection.execute(
                        f"DELETE FROM {table} WHERE expires_at IS NOT NULL AND expires_at <= ?",
                        (stamp,),
                    )
                    deleted += cursor.rowcount if cursor.rowcount > 0 else 0
                deleted += self._expire_by_retention(connection, now)
                deleted += self._enforce_row_budget(connection)
                connection.execute("COMMIT")
            except Exception:
                with suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise
        deleted += self._enforce_size_budget()
        return deleted

    def read_checkpoint(self, consumer: str, source_id: str) -> CheckpointRecord | None:
        """One consumer's cursor in one source, or None when it has never run."""
        self._ensure_ready()
        self._check_consumer(consumer)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT consumer, source_id, state_version, state_json, generation, "
                "observed_at, expires_at FROM office_source_checkpoint "
                "WHERE consumer = ? AND source_id = ?",
                (consumer, source_id),
            ).fetchone()
        if row is None:
            return None
        return CheckpointRecord(
            consumer=_as_consumer(row["consumer"]),
            source_id=row["source_id"],
            state_version=int(row["state_version"]),
            state=_decode_scalars(row["state_json"]),
            generation=int(row["generation"]),
            observed_at=_require(row["observed_at"], "checkpoint observed_at"),
            expires_at=_parse(row["expires_at"]),
        )

    def write_checkpoint(
        self,
        consumer: str,
        source_id: str,
        expected_generation: int,
        state: Mapping[str, object],
        generation: int,
        observed_at: datetime,
        *,
        derived_updates: tuple[ObservationUpdate, ...] = (),
    ) -> bool:
        """Compare-and-set the cursor, committing its derived rows with it.

        False means the stored generation was not ``expected_generation`` and
        NOTHING was written — not the cursor and not one derived row. That is
        the contract P03 and P10 depend on to be able to re-read and retry: a
        losing writer must leave the world exactly as it found it.

        ``state_version`` is taken from the state itself when it carries one,
        so a consumer can evolve its cursor's shape without a migration here.
        """
        self._ensure_ready()
        self._check_consumer(consumer)
        if not source_id:
            raise StorageBoundsError("a checkpoint needs a server-resolved source id")
        if generation < expected_generation:
            raise StorageBoundsError(
                f"a checkpoint generation may not move backwards "
                f"({generation} < {expected_generation})"
            )
        scalars = _scalars(state, self._limits, "checkpoint state")
        encoded = self._encode(scalars, "checkpoint state")
        version = int(scalars.get("state_version", 1) or 1)
        stamp = _iso(observed_at)

        with self._lock, self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT generation FROM office_source_checkpoint "
                    "WHERE consumer = ? AND source_id = ?",
                    (consumer, source_id),
                ).fetchone()
                current = int(row["generation"]) if row is not None else 0
                if current != expected_generation:
                    connection.execute("ROLLBACK")
                    return False
                connection.execute(
                    "INSERT INTO office_source_checkpoint("
                    "consumer, source_id, state_version, state_json, generation, "
                    "observed_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, NULL) "
                    "ON CONFLICT(consumer, source_id) DO UPDATE SET "
                    "state_version = excluded.state_version, state_json = excluded.state_json, "
                    "generation = excluded.generation, observed_at = excluded.observed_at",
                    (consumer, source_id, version, encoded, generation, stamp),
                )
                for update in derived_updates:
                    self._apply_update(connection, update)
                connection.execute("COMMIT")
            except Exception:
                with suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise
        return True

    # -- correlations ------------------------------------------------------

    def upsert_correlation(self, record: CorrelationRecord) -> None:
        """Store or refresh one join record, keyed by its join tuple.

        The same join observed twice updates in place; a resumed session
        arrives with a different ``session_id`` and gets its own record, which
        is the intended distinction — the same conversation resumed is not the
        same run.
        """
        self._ensure_ready()
        evidence = self._encode(
            _scalars(record.join_evidence, self._limits, "join evidence"), "join evidence"
        )
        self._bounded(record.pipeline_marker, "pipeline_marker")
        payload = (
            record.resolved_id(),
            record.project_id,
            record.session_id,
            record.binding_id,
            record.binding_revision,
            record.pipeline_marker,
            record.run_id,
            record.status,
            evidence,
            _iso(record.observed_at),
            _iso(record.last_verified_at) if record.last_verified_at else None,
            _iso(record.expires_at) if record.expires_at else None,
            self._bounded(record.source, "source"),
        )
        with self._lock, self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO office_correlation("
                    "correlation_id, project_id, session_id, binding_id, binding_revision, "
                    "pipeline_marker, run_id, status, join_evidence_json, observed_at, "
                    "last_verified_at, expires_at, source) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(correlation_id) DO UPDATE SET "
                    "status = excluded.status, run_id = excluded.run_id, "
                    "pipeline_marker = excluded.pipeline_marker, "
                    "join_evidence_json = excluded.join_evidence_json, "
                    "last_verified_at = excluded.last_verified_at, "
                    "expires_at = excluded.expires_at, source = excluded.source, "
                    "observed_at = excluded.observed_at "
                    "WHERE excluded.observed_at >= office_correlation.observed_at",
                    payload,
                )
            except sqlite3.IntegrityError as exc:
                raise StorageError(
                    "a different correlation id already records this join "
                    "(project, session, binding revision, run)"
                ) from exc

    def correlations_for(
        self, *, project_id: str, session_id: str | None = None
    ) -> tuple[CorrelationRecord, ...]:
        """Every join record for a project, newest first; optionally one session.

        Verified, unjoined and stale records all come back. P13 needs the
        difference — "we looked and there is no run" is an answer it must be
        able to show without re-querying the platform.
        """
        self._ensure_ready()
        sql = (
            "SELECT correlation_id, project_id, session_id, binding_id, binding_revision, "
            "pipeline_marker, run_id, status, join_evidence_json, observed_at, "
            "last_verified_at, expires_at, source FROM office_correlation WHERE project_id = ?"
        )
        params: list[object] = [project_id]
        if session_id is not None:
            sql += " AND session_id = ?"
            params.append(session_id)
        sql += " ORDER BY observed_at DESC, correlation_id"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return tuple(
            CorrelationRecord(
                correlation_id=row["correlation_id"],
                project_id=row["project_id"],
                session_id=row["session_id"],
                binding_id=row["binding_id"],
                binding_revision=int(row["binding_revision"]),
                pipeline_marker=row["pipeline_marker"],
                run_id=row["run_id"],
                status=_as_status(row["status"]),
                join_evidence=_decode_scalars(row["join_evidence_json"]),
                observed_at=_require(row["observed_at"], "correlation observed_at"),
                last_verified_at=_parse(row["last_verified_at"]),
                expires_at=_parse(row["expires_at"]),
                source=row["source"],
            )
            for row in rows
        )

    def project_observations(self) -> tuple[ProjectObservation, ...]:
        """The bounded project facts, since :meth:`read` cannot carry them."""
        self._ensure_ready()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT project_id, name, root_label, codename, frozen, board_seq, "
                "observed_at, expires_at, source, generation FROM office_project_observation "
                "ORDER BY project_id"
            ).fetchall()
        return tuple(
            ProjectObservation(
                project_id=row["project_id"],
                name=row["name"],
                root_label=row["root_label"],
                codename=row["codename"],
                frozen=None if row["frozen"] is None else bool(row["frozen"]),
                board_seq=None if row["board_seq"] is None else int(row["board_seq"]),
                observed_at=_parse(row["observed_at"]),
                expires_at=_parse(row["expires_at"]),
                source=row["source"],
                generation=int(row["generation"]),
            )
            for row in rows
        )

    def derived_observations(
        self, *, category: ObservationCategory | None = None, now: datetime | None = None
    ) -> tuple[ObservationUpdate, ...]:
        """The derived facts written beside a checkpoint move, newest first.

        The read side of :meth:`write_checkpoint`'s ``derived_updates``: P10
        writes a cost fact with its cursor and P04 needs to find it again after
        a restart. Expired rows are excluded when ``now`` is supplied, which is
        how a caller distinguishes "not written" from "written and aged out".
        """
        self._ensure_ready()
        sql = (
            "SELECT key, category, payload_json, observed_at, expires_at, generation "
            "FROM office_observation_meta WHERE 1 = 1"
        )
        params: list[object] = []
        if category is not None:
            sql += " AND category = ?"
            params.append(category)
        if now is not None:
            sql += " AND (expires_at IS NULL OR expires_at > ?)"
            params.append(_iso(now))
        sql += " ORDER BY observed_at DESC, key"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return tuple(
            ObservationUpdate(
                category=_as_category(row["category"]),
                key=row["key"],
                payload=_decode_scalars(row["payload_json"]),
                generation=int(row["generation"]),
                observed_at=_require(row["observed_at"], "observation observed_at"),
                expires_at=_parse(row["expires_at"]),
            )
            for row in rows
        )

    # -- internals ---------------------------------------------------------

    def _ensure_ready(self) -> None:
        self._check_open()
        if not self._ready:
            self.migrate()

    def _check_open(self) -> None:
        if self._closed:
            raise StorageError("the sidecar is closed")

    def _check_consumer(self, consumer: str) -> None:
        if consumer not in CHECKPOINT_CONSUMERS:
            allowed = ", ".join(sorted(CHECKPOINT_CONSUMERS))
            raise StorageBoundsError(f"unknown checkpoint consumer; allowed: {allowed}")

    @contextmanager
    def _connect(
        self, *, migrating: bool = False, check_closed: bool = True
    ) -> Iterator[sqlite3.Connection]:
        """One short-lived connection, configured and then closed.

        ``isolation_level=None`` because every transaction here is explicit:
        the driver's implicit BEGIN would wrap statements this code has already
        decided the boundaries of, and a compare-and-set whose read and write
        are in different transactions is not a compare-and-set.
        """
        if check_closed:
            self._check_open()
        if migrating or not self._path.exists():
            self._prepare_location()
        try:
            connection = sqlite3.connect(
                str(self._path),
                timeout=self._limits.busy_timeout_ms / 1000,
                isolation_level=None,
            )
        except sqlite3.Error as exc:
            raise StorageUnavailable("the sidecar could not be opened") from exc
        connection.row_factory = sqlite3.Row
        try:
            connection.execute(f"PRAGMA busy_timeout = {self._limits.busy_timeout_ms}")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.create_function("source_rank", 1, _source_rank, deterministic=True)
            self._restrict_permissions()
            yield connection
        except sqlite3.DatabaseError as exc:
            raise StorageUnavailable("the sidecar is not usable") from exc
        finally:
            with suppress(sqlite3.Error):
                connection.close()

    def _prepare_location(self) -> None:
        """Owner-only directory, created before the database file exists."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name == "posix":
                self._path.parent.chmod(stat.S_IRWXU)
        except OSError as exc:
            raise StorageUnavailable(
                "the sidecar directory could not be created or secured"
            ) from exc

    def _restrict_permissions(self) -> None:
        """0600 on the database and its WAL siblings, where the platform has modes.

        Best effort by design: a filesystem without POSIX modes (or a mount
        that ignores them) is a real deployment, and refusing to run there
        would trade a working office for a permission bit the filesystem was
        never going to honour. Where modes exist they are applied.
        """
        if os.name != "posix":
            return
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self._path) + suffix)
            with suppress(OSError):
                if candidate.exists():
                    candidate.chmod(stat.S_IRUSR | stat.S_IWUSR)

    # -- merge helpers -----------------------------------------------------

    def _merge_project(
        self, connection: sqlite3.Connection, project: object, board_seq: int, observed: datetime
    ) -> None:
        project_id = str(getattr(project, "id", "") or "")
        if not project_id:
            raise StorageBoundsError("a project observation needs an id")
        root = getattr(project, "root", None)
        root_label = Path(str(root)).name if root is not None else project_id
        codename = getattr(project, "codename", None)
        connection.execute(
            "INSERT INTO office_project_observation("
            "project_id, name, root_label, codename, frozen, board_seq, observed_at, "
            "expires_at, source, generation) VALUES (?, ?, ?, ?, NULL, ?, ?, NULL, ?, ?) "
            "ON CONFLICT(project_id) DO UPDATE SET "
            "name = excluded.name, root_label = excluded.root_label, "
            "codename = excluded.codename, board_seq = excluded.board_seq, "
            "observed_at = excluded.observed_at, source = excluded.source, "
            "generation = excluded.generation " + _WINS.format(table="office_project_observation"),
            (
                project_id,
                self._bounded(codename or root_label, "project name"),
                self._bounded(root_label, "root_label"),
                self._bounded(codename, "codename") if codename else None,
                board_seq,
                _iso(observed),
                DEFAULT_SOURCE,
                board_seq,
            ),
        )

    def _merge_session(
        self,
        connection: sqlite3.Connection,
        session: TeamSession,
        board_seq: int,
        observed: datetime,
    ) -> None:
        # transcript_path is READ from the model and deliberately not written:
        # it is a filesystem path, and the sidecar's rule is that a path never
        # becomes a stored fact something downstream could dereference.
        connection.execute(
            "INSERT INTO office_session_observation("
            "session_id, project_id, agent_id, provider, role, label, focus, state, "
            "started_at, ended_at, last_activity_at, cursor, model, effort, account, "
            "evidence_json, observed_at, expires_at, source, generation) "
            "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "project_id = excluded.project_id, role = excluded.role, label = excluded.label, "
            "focus = excluded.focus, state = excluded.state, ended_at = excluded.ended_at, "
            "last_activity_at = excluded.last_activity_at, cursor = excluded.cursor, "
            "model = excluded.model, effort = excluded.effort, account = excluded.account, "
            "observed_at = excluded.observed_at, source = excluded.source, "
            "generation = excluded.generation " + _WINS.format(table="office_session_observation"),
            (
                session.id,
                session.project_id,
                self._bounded(_provider_of(session), "provider"),
                self._bounded(session.role, "role"),
                self._bounded(session.label, "label") if session.label else None,
                self._bounded(session.focus, "focus") if session.focus else None,
                self._bounded(session.state, "state"),
                _iso(session.started_at),
                _iso(session.ended_at) if session.ended_at else None,
                _iso(session.last_seen_at),
                int(session.cursor),
                self._bounded(session.model, "model") if session.model else None,
                self._bounded(session.effort, "effort") if session.effort else None,
                self._bounded(session.account, "account") if session.account else None,
                _iso(observed),
                DEFAULT_SOURCE,
                board_seq,
            ),
        )

    def _merge_pane(
        self, connection: sqlite3.Connection, pane: PaneObservation, board_seq: int
    ) -> None:
        lines = tuple(
            line[: self._limits.max_line_chars]
            for line in pane.lines[-self._limits.max_pane_lines :]
        )
        tail = self._encode(list(lines), "pane tail")
        digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()
        facts = self._encode(_facts_payload(pane.facts), "pane facts") if pane.facts else None
        connection.execute(
            "INSERT INTO office_pane_observation("
            "agent_id, alive, health, exit_status, tail_json, tail_hash, facts_json, "
            "capture_generation, observed_at, expires_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?) "
            "ON CONFLICT(agent_id) DO UPDATE SET "
            "alive = excluded.alive, health = excluded.health, "
            "exit_status = excluded.exit_status, tail_json = excluded.tail_json, "
            "tail_hash = excluded.tail_hash, facts_json = excluded.facts_json, "
            "capture_generation = excluded.capture_generation, "
            "observed_at = excluded.observed_at, source = excluded.source "
            "WHERE excluded.capture_generation > office_pane_observation.capture_generation "
            "OR (excluded.capture_generation = office_pane_observation.capture_generation "
            "AND excluded.observed_at >= office_pane_observation.observed_at)",
            (
                pane.agent_id,
                int(pane.alive),
                self._bounded(pane.health, "pane health"),
                pane.exit_status,
                tail,
                digest,
                facts,
                board_seq,
                _iso(pane.observed_at),
                DEFAULT_SOURCE,
            ),
        )

    def _merge_prompt(self, connection: sqlite3.Connection, prompt: PromptEvidence) -> None:
        options = self._encode([option.to_wire() for option in prompt.options], "prompt options")
        questions = self._encode(
            [question.to_wire() for question in prompt.questions], "prompt questions"
        )
        selection = (
            None
            if prompt.selection is None
            else self._encode(list(prompt.selection), "prompt selection")
        )
        connection.execute(
            "INSERT INTO office_prompt_observation("
            "agent_id, generation, prompt_id, kind, provider, detected_by, options_json, "
            "questions_json, selection_json, raw, stale, observed_at, expires_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?) "
            "ON CONFLICT(agent_id, generation) DO UPDATE SET "
            "prompt_id = excluded.prompt_id, kind = excluded.kind, "
            "detected_by = excluded.detected_by, options_json = excluded.options_json, "
            "questions_json = excluded.questions_json, selection_json = excluded.selection_json, "
            "raw = excluded.raw, stale = excluded.stale, observed_at = excluded.observed_at, "
            "source = excluded.source "
            "WHERE excluded.observed_at >= office_prompt_observation.observed_at",
            (
                prompt.agent_id,
                int(prompt.generation),
                self._bounded(prompt.prompt_id, "prompt id"),
                self._bounded(prompt.kind, "prompt kind"),
                self._bounded(prompt.provider, "provider"),
                self._bounded(prompt.detected_by, "detected_by"),
                options,
                questions,
                selection,
                _redact(prompt.raw, self._limits.max_raw_chars),
                int(prompt.stale),
                _iso(prompt.observed_at),
                "hook" if prompt.detected_by == "hook" else DEFAULT_SOURCE,
            ),
        )

    def _merge_hook(self, connection: sqlite3.Connection, fact: HookFact, board_seq: int) -> None:
        connection.execute(
            "INSERT INTO office_hook_observation("
            "agent_id, name, value, metadata_json, generation, observed_at, expires_at, source) "
            "VALUES (?, ?, ?, NULL, ?, ?, NULL, 'hook') "
            "ON CONFLICT(agent_id, name) DO UPDATE SET "
            "value = excluded.value, generation = excluded.generation, "
            "observed_at = excluded.observed_at, source = excluded.source "
            "WHERE excluded.generation > office_hook_observation.generation "
            "OR (excluded.generation = office_hook_observation.generation "
            "AND excluded.observed_at >= office_hook_observation.observed_at)",
            (
                fact.agent_id,
                self._bounded(fact.name, "hook name"),
                self._bounded(fact.value, "hook value") if fact.value is not None else None,
                board_seq,
                _iso(fact.observed_at),
            ),
        )

    def _apply_update(self, connection: sqlite3.Connection, update: ObservationUpdate) -> None:
        if update.category not in OBSERVATION_CATEGORIES:
            allowed = ", ".join(sorted(OBSERVATION_CATEGORIES))
            raise StorageBoundsError(f"unknown observation category; allowed: {allowed}")
        payload = self._encode(
            _scalars(update.payload, self._limits, "observation payload"), "observation payload"
        )
        connection.execute(
            "INSERT INTO office_observation_meta("
            "key, category, payload_json, observed_at, expires_at, source, generation) "
            "VALUES (?, ?, ?, ?, ?, 'derived', ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "category = excluded.category, payload_json = excluded.payload_json, "
            "observed_at = excluded.observed_at, expires_at = excluded.expires_at, "
            "source = excluded.source, generation = excluded.generation "
            + _WINS.format(table="office_observation_meta"),
            (
                self._bounded(update.key, "observation key"),
                update.category,
                payload,
                _iso(update.observed_at),
                _iso(update.expires_at) if update.expires_at else None,
                int(update.generation),
            ),
        )

    # -- read helpers ------------------------------------------------------

    def _read_sessions(
        self, connection: sqlite3.Connection, cutoff: str
    ) -> tuple[TeamSession, ...]:
        rows = connection.execute(
            "SELECT * FROM office_session_observation "
            "WHERE expires_at IS NULL OR expires_at > ? ORDER BY session_id",
            (cutoff,),
        ).fetchall()
        return tuple(
            TeamSession(
                id=row["session_id"],
                project_id=row["project_id"],
                role=row["role"] or "unassigned",
                label=row["label"],
                focus=row["focus"],
                started_at=_require(row["started_at"], "session started_at"),
                last_seen_at=_parse(row["last_activity_at"])
                or _require(row["observed_at"], "session observed_at"),
                ended_at=_parse(row["ended_at"]),
                cursor=int(row["cursor"]),
                state=row["state"],
                transcript_path=None,
                account=row["account"],
                model=row["model"],
                effort=row["effort"],
            )
            for row in rows
        )

    def _read_panes(
        self, connection: sqlite3.Connection, cutoff: str
    ) -> tuple[PaneObservation, ...]:
        rows = connection.execute(
            "SELECT * FROM office_pane_observation "
            "WHERE expires_at IS NULL OR expires_at > ? ORDER BY agent_id",
            (cutoff,),
        ).fetchall()
        return tuple(
            PaneObservation(
                agent_id=row["agent_id"],
                alive=bool(row["alive"]),
                health=_as_health(row["health"]),
                observed_at=_require(row["observed_at"], "pane observed_at"),
                lines=tuple(str(line) for line in json.loads(row["tail_json"])),
                facts=_facts_from(row["facts_json"]),
                exit_status=row["exit_status"],
            )
            for row in rows
        )

    def _read_prompts(
        self, connection: sqlite3.Connection, cutoff: str
    ) -> tuple[PromptEvidence, ...]:
        # The LATEST lifecycle per agent, not every one retained. An older
        # generation is kept on disk so a stale answer can still be recognised
        # as stale, but it is not a current observation of anything.
        rows = connection.execute(
            "SELECT p.* FROM office_prompt_observation p JOIN ("
            "  SELECT agent_id, MAX(generation) AS generation FROM office_prompt_observation"
            "  WHERE expires_at IS NULL OR expires_at > ? GROUP BY agent_id"
            ") latest ON latest.agent_id = p.agent_id AND latest.generation = p.generation "
            "WHERE p.expires_at IS NULL OR p.expires_at > ? ORDER BY p.agent_id",
            (cutoff, cutoff),
        ).fetchall()
        return tuple(
            PromptEvidence(
                agent_id=row["agent_id"],
                provider=row["provider"],
                prompt_id=row["prompt_id"],
                generation=int(row["generation"]),
                kind=_as_kind(row["kind"]),
                detected_by=_as_detected_by(row["detected_by"]),
                observed_at=_require(row["observed_at"], "prompt observed_at"),
                options=tuple(
                    QuestionOption.model_validate(item)
                    for item in json.loads(row["options_json"] or "[]")
                ),
                questions=tuple(
                    AskQuestion.model_validate(item)
                    for item in json.loads(row["questions_json"] or "[]")
                ),
                selection=(
                    None
                    if row["selection_json"] is None
                    else tuple(int(index) for index in json.loads(row["selection_json"]))
                ),
                raw=row["raw"],
                stale=bool(row["stale"]),
            )
            for row in rows
        )

    def _read_hooks(self, connection: sqlite3.Connection, cutoff: str) -> tuple[HookFact, ...]:
        rows = connection.execute(
            "SELECT agent_id, name, value, observed_at FROM office_hook_observation "
            "WHERE expires_at IS NULL OR expires_at > ? ORDER BY agent_id, name",
            (cutoff,),
        ).fetchall()
        return tuple(
            HookFact(
                agent_id=row["agent_id"],
                name=row["name"],
                value=row["value"],
                observed_at=_require(row["observed_at"], "hook observed_at"),
            )
            for row in rows
        )

    # -- retention ---------------------------------------------------------

    def _expire_by_retention(self, connection: sqlite3.Connection, now: datetime) -> int:
        limits = self._limits
        deleted = 0
        horizon = now.timestamp()

        def before(seconds: float) -> str:
            return _iso(datetime.fromtimestamp(horizon - seconds, tz=UTC))

        # ENDED sessions only. A live session's row is never aged out, however
        # long the agent has been running.
        statements = (
            (
                "DELETE FROM office_session_observation "
                "WHERE ended_at IS NOT NULL AND ended_at < ?",
                before(limits.session_retention_s),
            ),
            (
                "DELETE FROM office_pane_observation WHERE observed_at < ?",
                before(limits.pane_retention_s),
            ),
            (
                "DELETE FROM office_prompt_observation WHERE observed_at < ?",
                before(limits.prompt_retention_s),
            ),
            (
                "DELETE FROM office_hook_observation WHERE observed_at < ?",
                before(limits.hook_retention_s),
            ),
            (
                "DELETE FROM office_correlation WHERE observed_at < ?",
                before(limits.correlation_retention_s),
            ),
        )
        for sql, stamp in statements:
            cursor = connection.execute(sql, (stamp,))
            deleted += cursor.rowcount if cursor.rowcount > 0 else 0
        return deleted

    def _enforce_row_budget(self, connection: sqlite3.Connection) -> int:
        """Oldest-first, per table, down to the row ceiling.

        Checkpoints are exempt: a cursor is not an observation, and dropping
        one silently restarts a consumer from the beginning of its source.
        """
        deleted = 0
        budget = self._limits.max_rows_per_table
        for table, key in (
            ("office_observation_meta", "key"),
            ("office_project_observation", "project_id"),
            ("office_session_observation", "session_id"),
            ("office_pane_observation", "agent_id"),
            ("office_prompt_observation", "rowid"),
            ("office_hook_observation", "rowid"),
            ("office_correlation", "correlation_id"),
        ):
            cursor = connection.execute(
                f"DELETE FROM {table} WHERE {key} IN ("
                f"  SELECT {key} FROM {table} ORDER BY observed_at DESC, {key} LIMIT -1 OFFSET ?"
                f")",
                (budget,),
            )
            deleted += cursor.rowcount if cursor.rowcount > 0 else 0
        return deleted

    def _enforce_size_budget(self) -> int:
        """Checkpoint the WAL when the file is over budget; report nothing removed.

        Bounded and honest: the WAL is truncated, which is the reclaimable part
        of an oversized sidecar, and the caller is not told rows were deleted
        when none were. Trimming live facts to hit a byte target would delete
        evidence for a reason the evidence had nothing to do with; if the
        budget is still exceeded afterwards, that is a condition for a
        coordinator-owned recovery packet, not for silent data loss here.
        """
        with suppress(OSError):
            if self._path.stat().st_size <= self._limits.max_database_bytes:
                return 0
        with suppress(sqlite3.Error, OSError), self._connect() as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return 0

    # -- bounds ------------------------------------------------------------

    def _bounded(self, value: str | None, what: str) -> str:
        text = "" if value is None else str(value)
        if len(text) > self._limits.max_text_chars:
            raise StorageBoundsError(
                f"{what} is {len(text)} characters, over the {self._limits.max_text_chars} "
                f"character limit"
            )
        return text

    def _encode(self, value: object, what: str) -> str:
        """Canonical JSON, refused past the byte ceiling.

        ``sort_keys`` so the same fact encodes to the same bytes every time:
        the pane tail hash and any future content comparison depend on
        stability, not on dictionary insertion order.
        """
        try:
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise StorageBoundsError(f"{what} is not encodable as bounded JSON") from exc
        size = len(encoded.encode())
        if size > self._limits.max_json_bytes:
            raise StorageBoundsError(
                f"{what} encodes to {size} bytes, over the {self._limits.max_json_bytes} byte limit"
            )
        return encoded


#: The ordering rule, as a SQL suffix: generation, then observation time, then
#: a fixed source rank. Shared by every table whose writers race.
_WINS: Final = (
    "WHERE excluded.generation > {table}.generation "
    "OR (excluded.generation = {table}.generation "
    "AND excluded.observed_at > {table}.observed_at) "
    "OR (excluded.generation = {table}.generation "
    "AND excluded.observed_at = {table}.observed_at "
    "AND source_rank(excluded.source) <= source_rank({table}.source))"
)


def _validated_path(path: Path, home: Path | None) -> Path:
    """Refuse a sidecar path that escapes the home, or that is the CLI's store."""
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        raise StoragePathError("the sidecar path must be absolute")
    if resolved.name == _CONTEXT_DB_NAME:
        raise StoragePathError("the sidecar is never the CLI's context.db")
    if home is not None:
        boundary = Path(home).expanduser()
        try:
            inside = resolved.is_relative_to(boundary)
        except ValueError:  # pragma: no cover - is_relative_to does not raise on Path
            inside = False
        if not inside:
            raise StoragePathError(
                "the sidecar path is outside the resolved AISQUARE home and was refused"
            )
    return resolved


def _provider_of(session: TeamSession) -> str:
    """The provider a session's model implies, bounded to a known word.

    ``TeamSession`` has no provider column; what it has is the model id the
    session reported. Anything unrecognised is ``unknown`` rather than a guess,
    because a wrong provider sends P09 to the wrong input planner.
    """
    model = (session.model or "").lower()
    if "claude" in model or model.startswith(("opus", "sonnet", "haiku", "fable")):
        return "claude"
    return "unknown"


def _facts_payload(facts: TerminalPaneFacts) -> dict[str, int | bool | None]:
    return {
        "width": facts.width,
        "height": facts.height,
        "cursor_x": facts.cursor_x,
        "cursor_y": facts.cursor_y,
        "cursor_visible": facts.cursor_visible,
        "alternate_on": facts.alternate_on,
        "history_size": facts.history_size,
        "dead": facts.dead,
        "dead_status": facts.dead_status,
        "in_mode": facts.in_mode,
    }


def _facts_from(encoded: str | None) -> TerminalPaneFacts | None:
    if not encoded:
        return None
    data = json.loads(encoded)
    return TerminalPaneFacts(
        width=int(data["width"]),
        height=int(data["height"]),
        cursor_x=int(data["cursor_x"]),
        cursor_y=int(data["cursor_y"]),
        cursor_visible=bool(data["cursor_visible"]),
        alternate_on=bool(data["alternate_on"]),
        history_size=int(data["history_size"]),
        dead=bool(data["dead"]),
        dead_status=None if data["dead_status"] is None else int(data["dead_status"]),
        in_mode=bool(data["in_mode"]),
    )


def _scalars(
    mapping: Mapping[str, object], limits: StorageLimits, what: str
) -> dict[str, str | int | float | bool | None]:
    """Only scalars, only bounded keys and strings.

    A nested structure is refused rather than flattened: the checkpoint state
    and the derived payload are cursors and facts, and the shapes that arrive
    nested are the ones carrying a transcript, a body or a path.
    """
    out: dict[str, str | int | float | bool | None] = {}
    for key, value in mapping.items():
        name = str(key)
        if not name or len(name) > limits.max_text_chars:
            raise StorageBoundsError(f"{what} has a key over the {limits.max_text_chars} limit")
        if value is None or isinstance(value, (bool, int, float)):
            out[name] = value
            continue
        if isinstance(value, str):
            if len(value) > limits.max_text_chars:
                raise StorageBoundsError(
                    f"{what}[{name!r}] is {len(value)} characters, over the "
                    f"{limits.max_text_chars} character limit"
                )
            out[name] = value
            continue
        raise StorageBoundsError(f"{what}[{name!r}] must be a scalar, not {type(value).__name__}")
    return out


def _decode_scalars(encoded: str) -> Mapping[str, str | int | float | bool | None]:
    data = json.loads(encoded)
    if not isinstance(data, dict):  # pragma: no cover - only a hand-edited database
        raise StorageUnavailable("a stored payload is not an object")
    return dict(data)


def _redact(text: str | None, limit: int) -> str | None:
    """Bounded, path-stripped evidence.

    Truncation alone is not enough: the first 2000 characters of a permission
    prompt are exactly where the absolute path of the file being edited lives.
    Paths become ``<path>``; the rest is kept, because evidence with the nouns
    removed cannot be matched against a prompt later.
    """
    if text is None:
        return None
    return _PATH_LIKE.sub("<path>", text)[:limit]


def _as_consumer(value: str) -> CheckpointConsumer:
    if value not in CHECKPOINT_CONSUMERS:  # pragma: no cover - writes are checked
        raise StorageUnavailable("a stored checkpoint names an unknown consumer")
    return "activity" if value == "activity" else "cost"


def _as_category(value: str) -> ObservationCategory:
    if value not in OBSERVATION_CATEGORIES:  # pragma: no cover - writes are checked
        raise StorageUnavailable("a stored observation names an unknown category")
    return value  # type: ignore[return-value]


def _as_status(value: str) -> CorrelationStatus:
    if value == "verified":
        return "verified"
    if value == "stale":
        return "stale"
    return "unjoined"


def _as_health(value: str) -> PaneHealth:
    return value if value in ("live", "dead", "gone", "unknown") else "unknown"  # type: ignore[return-value]


def _as_kind(value: str) -> QuestionKind:
    known: Sequence[str] = ("permission", "plan", "ask", "form", "continue", "question", "note")
    return value if value in known else "question"  # type: ignore[return-value]


def _as_detected_by(value: str) -> DetectedBy:
    return value if value in ("hook", "frame", "both") else "frame"  # type: ignore[return-value]


__all__ = [
    "CHECKPOINT_CONSUMERS",
    "OBSERVATION_CATEGORIES",
    "CorrelationRecord",
    "CorrelationStatus",
    "ObservationCategory",
    "ObservationDatabase",
    "ProjectObservation",
    "StorageBoundsError",
    "StorageError",
    "StorageLimits",
    "StorageMigrationError",
    "StoragePathError",
    "StorageUnavailable",
    "register_migration",
]
