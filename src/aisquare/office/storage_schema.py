"""The sidecar's schema, its limits, and the seam a later packet extends.

Three things live here, deliberately apart from the database object that uses
them.

**The migration registry.** ``register_migration`` is declarative: a version, a
name and a callable. P02 registers ``001``; P06 registers ``002`` for its
operation tables without importing a connection, a cursor or anything else
private to :mod:`aisquare.office.storage`. The registry validates strictly
increasing versions at registration time rather than at apply time, because a
duplicate or out-of-order version is a packaging mistake and the useful moment
to say so is import, not the first open on a user's machine.

**Migration 001's statements.** Observation, correlation and checkpoint tables
only. Nothing here pre-creates, reserves or hints at an operation table: the
ownership split is the point, and a column added "for P06" would be a column
P06 then has to migrate around.

**The bounds.** :class:`StorageLimits` holds every ceiling the sidecar
enforces, as one injectable value. A test drives tiny limits instead of writing
a megabyte to prove the megabyte is refused, and an operator-visible default
that changes has exactly one place to change.

The schema version lives in a TABLE rather than in ``PRAGMA user_version``.
``core.store`` uses the pragma and that is right for it — its migrations are a
list this code does not own — but here two packets write the list from two
different modules, and a row per applied migration records WHICH migration a
version number meant. A pragma integer cannot tell a half-integrated P06 from a
P02-only database; a table with ``(version, name, applied_at)`` can.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Final

SCHEMA_TABLE: Final = "office_schema"
"""One row per applied migration: the version, its name, and when it landed."""

OBSERVATION_TABLES: Final = (
    "office_observation_meta",
    "office_project_observation",
    "office_session_observation",
    "office_pane_observation",
    "office_prompt_observation",
    "office_hook_observation",
    "office_correlation",
    "office_source_checkpoint",
)
"""Every table migration 001 creates. P06's operation tables are NOT here."""

MigrationApply = Callable[[sqlite3.Connection], None]


class StorageError(RuntimeError):
    """A typed sidecar failure.

    Typed because the alternative is a ``sqlite3`` exception crossing into the
    Office worker and, from there, into a response: SQLite's messages carry the
    database path, and a path is exactly what never reaches a browser. The
    original exception is kept as ``__cause__`` for a local diagnostic and is
    never part of the message.
    """


class StoragePathError(StorageError):
    """The sidecar path is outside the resolved home, or is the CLI's store."""


class StorageBoundsError(StorageError):
    """A payload, row or database exceeded a configured ceiling."""


class StorageMigrationError(StorageError):
    """A migration could not be registered or could not be applied."""


class StorageUnavailable(StorageError):
    """The database cannot be used: corrupt, locked out, or newer than this code.

    Separate from the others because the caller's response differs. A bounds
    error is one bad fact to drop; this is "the sidecar is not available", which
    P05 surfaces as a degraded office rather than retrying forever — and which
    must never be answered by silently falling back to ``context.db``.
    """


@dataclass(frozen=True, slots=True)
class StorageLimits:
    """Every ceiling the sidecar enforces, in one injectable value.

    The defaults are starting points chosen to be comfortably larger than the
    facts the plan describes and comfortably smaller than a memory problem.
    They are not measured capacity, and nothing here claims a machine can hold
    them.
    """

    max_json_bytes: int = 16_384
    """Encoded bytes for one JSON column — evidence, not a transcript."""

    max_text_chars: int = 512
    """One bounded text column: a label, a name, a state, a hook name."""

    max_raw_chars: int = 2_000
    """Bounded pane text kept as prompt evidence, after redaction."""

    max_pane_lines: int = 200
    """Lines of tail kept per pane. P08 owns real scrollback; this is a tail."""

    max_line_chars: int = 500
    """One captured line. A pane can emit a single megabyte-long line."""

    max_rows_per_table: int = 5_000
    """Row budget per observation table, enforced oldest-first by ``expire``."""

    max_database_bytes: int = 64 * 1024 * 1024
    """Total size budget, checked after merge; over it, ``expire`` trims."""

    session_retention_s: float = 7 * 24 * 3600.0
    """How long an ENDED session's row survives. Live sessions are not aged."""

    pane_retention_s: float = 6 * 3600.0
    prompt_retention_s: float = 24 * 3600.0
    hook_retention_s: float = 24 * 3600.0
    correlation_retention_s: float = 30 * 24 * 3600.0

    busy_timeout_ms: int = 5_000
    """Finite on purpose. An unbounded wait on a locked sidecar is a hung poll,
    and the poll's own budget is 500 ms — the sidecar may not outlive it by
    minutes."""

    def __post_init__(self) -> None:
        for name in (
            "max_json_bytes",
            "max_text_chars",
            "max_raw_chars",
            "max_pane_lines",
            "max_line_chars",
            "max_rows_per_table",
            "max_database_bytes",
            "busy_timeout_ms",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"StorageLimits.{name} must be positive")
        for name in (
            "session_retention_s",
            "pane_retention_s",
            "prompt_retention_s",
            "hook_retention_s",
            "correlation_retention_s",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"StorageLimits.{name} must be positive")


@dataclass(frozen=True, slots=True)
class Migration:
    """One numbered, named schema change."""

    version: int
    name: str
    apply: MigrationApply


class MigrationRegistry:
    """The ordered list of migrations, and the rules a registration must meet.

    An object rather than a bare module list so a test can build its own
    registry — including one whose migration raises — without reaching into,
    and having to restore, global state. The process-wide default below is what
    P02 and P06 register against.
    """

    def __init__(self) -> None:
        self._migrations: list[Migration] = []

    def register(self, version: int, name: str, apply: MigrationApply) -> Migration:
        """Add a migration, or refuse for a reason that names the conflict."""
        if version <= 0:
            raise StorageMigrationError(f"migration version must be positive, got {version}")
        if not name:
            raise StorageMigrationError(f"migration {version} must have a name")
        for existing in self._migrations:
            if existing.version == version:
                raise StorageMigrationError(
                    f"migration {version} is already registered as {existing.name!r}; "
                    f"{name!r} must choose the next free version"
                )
        if self._migrations and version <= self._migrations[-1].version:
            raise StorageMigrationError(
                f"migration versions must strictly increase: {version} follows "
                f"{self._migrations[-1].version} ({self._migrations[-1].name!r})"
            )
        migration = Migration(version=version, name=name, apply=apply)
        self._migrations.append(migration)
        return migration

    def all(self) -> tuple[Migration, ...]:
        """Every registered migration, in registration (and version) order."""
        return tuple(self._migrations)

    def pending(self, applied: int) -> tuple[Migration, ...]:
        """The migrations past ``applied``, in order."""
        return tuple(m for m in self._migrations if m.version > applied)

    def latest_version(self) -> int:
        """The highest registered version, or 0 when nothing is registered."""
        return self._migrations[-1].version if self._migrations else 0

    def __iter__(self) -> Iterator[Migration]:
        return iter(self._migrations)

    def __len__(self) -> int:
        return len(self._migrations)


MIGRATIONS: Final = MigrationRegistry()
"""The process-wide registry. P02 fills 001 below; P06 appends 002."""


def register_migration(version: int, name: str, apply: MigrationApply) -> Migration:
    """Register against the default registry.

    The seam P06 uses::

        from aisquare.office.storage import register_migration

        def _operations(connection: sqlite3.Connection) -> None:
            connection.execute("CREATE TABLE office_operation (...)")

        register_migration(2, "operations", _operations)

    ``apply`` receives a connection already inside a transaction the sidecar
    opened and will commit; it must not ``BEGIN``, ``COMMIT`` or ``ROLLBACK``,
    because the version row and the schema change are one atomic unit and a
    nested commit would split them.
    """
    return MIGRATIONS.register(version, name, apply)


_SCHEMA_DDL: Final = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA_TABLE} (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


def ensure_schema_table(connection: sqlite3.Connection) -> None:
    """Create the version table if it is absent. Safe to call on every open."""
    connection.execute(_SCHEMA_DDL)


def applied_version(connection: sqlite3.Connection) -> int:
    """The highest applied migration version, or 0 on a fresh database."""
    row = connection.execute(f"SELECT MAX(version) FROM {SCHEMA_TABLE}").fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


#: Migration 001. Observation, correlation and checkpoint tables only.
#:
#: Every table carries ``observed_at`` and ``source``; every table a poll or a
#: hook can write carries ``generation`` too, because merge ordering compares
#: generation first and observation time second. The pairs are what make a late
#: hook unable to overwrite a newer poll — see ``storage._wins``.
_MIGRATION_001: Final[Sequence[str]] = (
    # One row per observed key: the generic bucket P03/P10 write derived facts
    # into alongside a checkpoint move. Categories are bounded by P01's
    # ObservationCategory; the column is TEXT because a CHECK constraint here
    # would have to be migrated every time that Literal gains a member.
    """
    CREATE TABLE office_observation_meta (
        key          TEXT PRIMARY KEY,
        category     TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        observed_at  TEXT NOT NULL,
        expires_at   TEXT,
        source       TEXT NOT NULL,
        generation   INTEGER NOT NULL
    )
    """,
    "CREATE INDEX ix_observation_meta_category ON office_observation_meta(category, observed_at)",
    "CREATE INDEX ix_observation_meta_expiry ON office_observation_meta(expires_at)",
    # root_label, never root. The CLI store owns the real path; what is kept
    # here is a display label, so nothing downstream can turn a sidecar row
    # into a filesystem argument.
    """
    CREATE TABLE office_project_observation (
        project_id  TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        root_label  TEXT NOT NULL,
        codename    TEXT,
        frozen      INTEGER,
        board_seq   INTEGER,
        observed_at TEXT NOT NULL,
        expires_at  TEXT,
        source      TEXT NOT NULL,
        generation  INTEGER NOT NULL
    )
    """,
    "CREATE INDEX ix_project_observation_expiry ON office_project_observation(expires_at)",
    # agent_id is nullable on purpose: a session observed from the board before
    # its fleet row exists has no agent, and guessing one is how two unrelated
    # rows get joined. No foreign key for the same reason — a partial startup
    # fact must be storable before the row it would reference.
    """
    CREATE TABLE office_session_observation (
        session_id       TEXT PRIMARY KEY,
        project_id       TEXT NOT NULL,
        agent_id         TEXT,
        provider         TEXT,
        role             TEXT,
        label            TEXT,
        focus            TEXT,
        state            TEXT NOT NULL,
        started_at       TEXT NOT NULL,
        ended_at         TEXT,
        last_activity_at TEXT,
        cursor           INTEGER NOT NULL DEFAULT 0,
        model            TEXT,
        effort           TEXT,
        account          TEXT,
        evidence_json    TEXT,
        observed_at      TEXT NOT NULL,
        expires_at       TEXT,
        source           TEXT NOT NULL,
        generation       INTEGER NOT NULL
    )
    """,
    """
    CREATE INDEX ix_session_observation_project
        ON office_session_observation(project_id, observed_at)
    """,
    "CREATE INDEX ix_session_observation_expiry ON office_session_observation(expires_at)",
    # The agent key is server-resolved. A browser-supplied pane id never
    # reaches this column: P08 resolves a target and P03 supplies the opaque
    # agent identity, so nothing here is addressable from outside.
    """
    CREATE TABLE office_pane_observation (
        agent_id           TEXT PRIMARY KEY,
        alive              INTEGER NOT NULL,
        health             TEXT NOT NULL,
        exit_status        INTEGER,
        tail_json          TEXT NOT NULL,
        tail_hash          TEXT NOT NULL,
        facts_json         TEXT,
        capture_generation INTEGER NOT NULL,
        observed_at        TEXT NOT NULL,
        expires_at         TEXT,
        source             TEXT NOT NULL
    )
    """,
    "CREATE INDEX ix_pane_observation_expiry ON office_pane_observation(expires_at)",
    # Keyed by agent AND lifecycle generation, so the same text reappearing
    # after a pane clears is a second row rather than an overwrite. That is
    # what lets P09 refuse an answer prepared against the earlier lifecycle
    # instead of silently sending it to the new prompt.
    """
    CREATE TABLE office_prompt_observation (
        agent_id       TEXT NOT NULL,
        generation     INTEGER NOT NULL,
        prompt_id      TEXT NOT NULL,
        kind           TEXT NOT NULL,
        provider       TEXT NOT NULL,
        detected_by    TEXT NOT NULL,
        options_json   TEXT,
        questions_json TEXT,
        selection_json TEXT,
        raw            TEXT,
        stale          INTEGER NOT NULL DEFAULT 0,
        observed_at    TEXT NOT NULL,
        expires_at     TEXT,
        source         TEXT NOT NULL,
        PRIMARY KEY (agent_id, generation)
    )
    """,
    """
    CREATE INDEX ix_prompt_observation_agent
        ON office_prompt_observation(agent_id, observed_at)
    """,
    "CREATE INDEX ix_prompt_observation_expiry ON office_prompt_observation(expires_at)",
    # Hook facts, not TeamEvents. Nothing in this packet writes the CLI's event
    # table, and a fact here is bounded scalar evidence with a generation so a
    # slow hook cannot overwrite a newer one.
    """
    CREATE TABLE office_hook_observation (
        agent_id      TEXT NOT NULL,
        name          TEXT NOT NULL,
        value         TEXT,
        metadata_json TEXT,
        generation    INTEGER NOT NULL,
        observed_at   TEXT NOT NULL,
        expires_at    TEXT,
        source        TEXT NOT NULL,
        PRIMARY KEY (agent_id, name)
    )
    """,
    "CREATE INDEX ix_hook_observation_expiry ON office_hook_observation(expires_at)",
    # The workspace key is not here and cannot be: the columns are the resolved
    # binding's identity and revision, an opaque marker, and bounded evidence.
    """
    CREATE TABLE office_correlation (
        correlation_id     TEXT PRIMARY KEY,
        project_id         TEXT NOT NULL,
        session_id         TEXT,
        binding_id         TEXT NOT NULL,
        binding_revision   INTEGER NOT NULL,
        pipeline_marker    TEXT NOT NULL,
        run_id             TEXT,
        status             TEXT NOT NULL,
        join_evidence_json TEXT NOT NULL,
        observed_at        TEXT NOT NULL,
        last_verified_at   TEXT,
        expires_at         TEXT,
        source             TEXT NOT NULL
    )
    """,
    # IFNULL in the index, not a plain UNIQUE: SQLite treats NULLs as distinct,
    # so a UNIQUE over nullable session_id/run_id would happily store the same
    # unjoined correlation a hundred times. A resumed session arrives with a
    # new session_id and still gets its own record, which is the intended
    # freedom; a repeat of the SAME join does not.
    """
    CREATE UNIQUE INDEX ux_correlation_join ON office_correlation(
        project_id,
        IFNULL(session_id, ''),
        binding_id,
        binding_revision,
        IFNULL(run_id, '')
    )
    """,
    """
    CREATE INDEX ix_correlation_project
        ON office_correlation(project_id, IFNULL(session_id, ''), observed_at)
    """,
    "CREATE INDEX ix_correlation_expiry ON office_correlation(expires_at)",
    # (consumer, source_id): activity and cost keep separate cursors over the
    # same source on purpose. One shared cursor would mean whichever consumer
    # ran first decided what the other was allowed to still see.
    """
    CREATE TABLE office_source_checkpoint (
        consumer      TEXT NOT NULL,
        source_id     TEXT NOT NULL,
        state_version INTEGER NOT NULL,
        state_json    TEXT NOT NULL,
        generation    INTEGER NOT NULL,
        observed_at   TEXT NOT NULL,
        expires_at    TEXT,
        PRIMARY KEY (consumer, source_id)
    )
    """,
    "CREATE INDEX ix_source_checkpoint_expiry ON office_source_checkpoint(expires_at)",
)


def apply_migration_001(connection: sqlite3.Connection) -> None:
    """Create the observation, correlation and checkpoint tables.

    Statements run one at a time rather than through ``executescript``: that
    method issues an implicit COMMIT before its script, which would release the
    transaction the caller opened and split the schema change from the version
    row that records it. ``core.store._migrate`` learned the same lesson and
    records it at length.
    """
    for statement in _MIGRATION_001:
        connection.execute(statement)


MIGRATION_001: Final = register_migration(1, "observations", apply_migration_001)
"""Registered at import. P06 appends ``register_migration(2, "operations", ...)``."""


__all__ = [
    "MIGRATIONS",
    "MIGRATION_001",
    "OBSERVATION_TABLES",
    "SCHEMA_TABLE",
    "Migration",
    "MigrationApply",
    "MigrationRegistry",
    "StorageBoundsError",
    "StorageError",
    "StorageLimits",
    "StorageMigrationError",
    "StoragePathError",
    "StorageUnavailable",
    "applied_version",
    "apply_migration_001",
    "ensure_schema_table",
    "register_migration",
]
