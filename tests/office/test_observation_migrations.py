"""Migration 001, and the seam P06 adds 002 through.

The property that matters here is not "the tables exist" — it is that a second
packet can extend this schema without editing this packet, and that a
migration which fails leaves a database somebody can still read. Both are
asserted against a registry the test builds itself, so a failing migration
never has to be registered globally to be exercised.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aisquare.office.config import OfficeConfig
from aisquare.office.models import LocalObservationBatch
from aisquare.office.storage import ObservationDatabase
from aisquare.office.storage_schema import (
    MIGRATIONS,
    OBSERVATION_TABLES,
    SCHEMA_TABLE,
    MigrationRegistry,
    StorageMigrationError,
    StorageUnavailable,
    apply_migration_001,
)

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


class FrozenClock:
    def now(self) -> datetime:
        return NOW

    def monotonic(self) -> float:
        return 0.0


def _registry_with_001() -> MigrationRegistry:
    """A private registry holding exactly what P02 ships."""
    registry = MigrationRegistry()
    registry.register(1, "observations", apply_migration_001)
    return registry


def _database(home: Path, registry: MigrationRegistry | None = None) -> ObservationDatabase:
    return ObservationDatabase.from_config(
        OfficeConfig(home=home), FrozenClock(), registry=registry
    )


def _tables(path: Path) -> set[str]:
    connection = sqlite3.connect(str(path))
    try:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    finally:
        connection.close()
    return {row[0] for row in rows}


def test_the_shipped_registry_holds_migration_001_and_nothing_of_p06s(tmp_path: Path) -> None:
    versions = [migration.version for migration in MIGRATIONS]
    names = [migration.name for migration in MIGRATIONS]

    assert versions[0] == 1
    assert names[0] == "observations"
    assert versions == sorted(set(versions)), "versions must be unique and increasing"


def test_migration_001_creates_the_observation_tables_and_records_its_version(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path / "home", _registry_with_001())

    version = database.migrate()

    tables = _tables(database.path)
    assert version == 1
    assert set(OBSERVATION_TABLES) <= tables
    assert SCHEMA_TABLE in tables
    assert not {name for name in tables if "operation" in name}, (
        "operation tables belong to P06's migration 002"
    )


def test_migration_001_records_the_name_beside_the_version(tmp_path: Path) -> None:
    database = _database(tmp_path / "home", _registry_with_001())
    database.migrate()

    connection = sqlite3.connect(str(database.path))
    try:
        rows = connection.execute(f"SELECT version, name FROM {SCHEMA_TABLE}").fetchall()
    finally:
        connection.close()

    assert rows == [(1, "observations")]


def test_migrating_twice_is_idempotent_and_keeps_the_data(tmp_path: Path) -> None:
    home = tmp_path / "home"
    database = _database(home, _registry_with_001())
    database.migrate()
    database.merge(LocalObservationBatch(collected_at=NOW, board_seq=8))

    second = _database(home, _registry_with_001())
    version = second.migrate()

    assert version == 1
    assert second.migrate() == 1
    assert second.read(now=NOW).board_seq == 8


def test_a_later_packet_can_add_migration_002_without_touching_this_one(tmp_path: Path) -> None:
    """Exactly the seam P06 uses: register, migrate, and 001's rows survive."""
    home = tmp_path / "home"
    registry = _registry_with_001()
    _database(home, registry).migrate()
    applied: list[str] = []

    def operations(connection: sqlite3.Connection) -> None:
        applied.append("002")
        connection.execute(
            "CREATE TABLE office_operation (operation_id TEXT PRIMARY KEY, status TEXT NOT NULL)"
        )

    registry.register(2, "operations", operations)
    database = _database(home, registry)
    version = database.migrate()

    tables = _tables(database.path)
    assert applied == ["002"]
    assert version == 2
    assert "office_operation" in tables
    assert set(OBSERVATION_TABLES) <= tables


def test_a_failed_migration_rolls_back_and_leaves_the_prior_version_usable(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    registry = _registry_with_001()
    _database(home, registry).migrate()

    def broken(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE office_half_built (id TEXT PRIMARY KEY)")
        raise RuntimeError("the migration changed its mind")

    registry.register(2, "half-built", broken)
    database = _database(home, registry)

    with pytest.raises(StorageMigrationError) as caught:
        database.migrate()

    assert "still at version 1" in str(caught.value)
    assert "office_half_built" not in _tables(database.path)
    readable = _database(home, _registry_with_001())
    assert readable.schema_version() == 1
    readable.merge(LocalObservationBatch(collected_at=NOW, board_seq=2))
    assert readable.read(now=NOW).board_seq == 2


def test_a_registration_refuses_a_duplicate_version() -> None:
    registry = _registry_with_001()

    with pytest.raises(StorageMigrationError) as caught:
        registry.register(1, "something-else", apply_migration_001)

    assert "already registered as 'observations'" in str(caught.value)


def test_a_registration_refuses_a_version_that_does_not_increase() -> None:
    registry = _registry_with_001()
    registry.register(5, "later", apply_migration_001)

    with pytest.raises(StorageMigrationError) as caught:
        registry.register(3, "earlier", apply_migration_001)

    assert "strictly increase" in str(caught.value)
    assert [migration.version for migration in registry] == [1, 5]


def test_a_registration_refuses_a_nonpositive_version_or_a_missing_name() -> None:
    registry = MigrationRegistry()

    with pytest.raises(StorageMigrationError, match="must be positive"):
        registry.register(0, "zero", apply_migration_001)
    with pytest.raises(StorageMigrationError, match="must have a name"):
        registry.register(1, "", apply_migration_001)


def test_a_database_newer_than_this_build_is_refused_rather_than_downgraded(
    tmp_path: Path,
) -> None:
    """A sidecar written by a future build is not readable here, and says so."""
    home = tmp_path / "home"
    registry = _registry_with_001()
    registry.register(2, "future", lambda connection: None)
    _database(home, registry).migrate()

    older_build = _database(home, _registry_with_001())

    with pytest.raises(StorageUnavailable) as caught:
        older_build.migrate()

    assert "newer than this build" in str(caught.value)


def test_a_pending_migration_is_applied_on_first_use_without_an_explicit_call(
    tmp_path: Path,
) -> None:
    """P05 calls read/merge, not migrate; the schema still has to be there."""
    database = _database(tmp_path / "home", _registry_with_001())

    batch = database.read(now=NOW)

    assert batch.board_seq == 0
    assert database.schema_version() == 1
