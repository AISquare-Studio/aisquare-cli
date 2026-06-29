"""SQLite-backed context store.

This is the single home of all persisted context. The public surface is the
``ContextStore`` protocol; ``open_store`` / ``store_session`` hand back the
SQLite implementation. Services depend on the protocol, never on SQLite
directly, so the backend can evolve without touching the service layer.

On-disk shape (``~/.aisquare/context.db``):

- ``entry``      — one row per remembered fact, in the ``user`` or ``project`` pool;
- ``entry_fts``  — an FTS5 mirror of entry text/tags kept current by triggers;
- ``project``    — projects referenced by ``project``-pool entries.

Deletes are soft: ``delete`` stamps ``deleted_at`` and leaves a tombstone so the
removal can propagate when sync lands. Every read filters tombstones out.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from aisquare.core import paths
from aisquare.core.ids import new_prompt_id
from aisquare.models import ContextEntry, Pool, ProjectInfo, PromptRecord

_SCHEMA_V1 = """
CREATE TABLE entry (
    id          TEXT PRIMARY KEY,
    pool        TEXT NOT NULL CHECK (pool IN ('user', 'project')),
    project_id  TEXT REFERENCES project (id),
    text        TEXT NOT NULL,
    tags        TEXT NOT NULL DEFAULT '[]',
    source      TEXT NOT NULL DEFAULT 'manual',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    deleted_at  TEXT,
    CHECK ((pool = 'project') = (project_id IS NOT NULL))
);
CREATE INDEX entry_pool_project ON entry (pool, project_id) WHERE deleted_at IS NULL;

CREATE TABLE project (
    id            TEXT PRIMARY KEY,
    root          TEXT NOT NULL,
    name          TEXT NOT NULL,
    linked_repos  TEXT NOT NULL DEFAULT '[]',
    created_at    TEXT NOT NULL
);

CREATE VIRTUAL TABLE entry_fts USING fts5 (
    text, tags, content='entry', content_rowid='rowid'
);
CREATE TRIGGER entry_ai AFTER INSERT ON entry BEGIN
    INSERT INTO entry_fts (rowid, text, tags) VALUES (new.rowid, new.text, new.tags);
END;
CREATE TRIGGER entry_ad AFTER DELETE ON entry BEGIN
    INSERT INTO entry_fts (entry_fts, rowid, text, tags)
    VALUES ('delete', old.rowid, old.text, old.tags);
END;
CREATE TRIGGER entry_au AFTER UPDATE ON entry BEGIN
    INSERT INTO entry_fts (entry_fts, rowid, text, tags)
    VALUES ('delete', old.rowid, old.text, old.tags);
    INSERT INTO entry_fts (rowid, text, tags) VALUES (new.rowid, new.text, new.tags);
END;
"""

# v2: capture of how the user prompts their agent, for replay and smarter context.
_SCHEMA_V2 = """
CREATE TABLE prompt (
    id          TEXT PRIMARY KEY,
    project_id  TEXT REFERENCES project (id),
    text        TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'claude-code',
    created_at  TEXT NOT NULL
);
CREATE INDEX prompt_project ON prompt (project_id, created_at);
"""

# Ordered migrations; index i upgrades the db from user_version i to i+1.
_MIGRATIONS = (_SCHEMA_V1, _SCHEMA_V2)
SCHEMA_VERSION = len(_MIGRATIONS)

_COLUMNS = "id, pool, project_id, text, tags, source, created_at, updated_at, deleted_at"
_PROMPT_COLUMNS = "id, project_id, text, source, created_at"


class AmbiguousIdError(LookupError):
    """Raised when a partial entry id matches more than one entry."""

    def __init__(self, ref: str) -> None:
        super().__init__(f"entry id {ref!r} is ambiguous")
        self.ref = ref


class ContextStore(Protocol):
    """Everything the service layer needs from persistent context storage."""

    def add(self, entry: ContextEntry) -> ContextEntry: ...
    def get(self, ref: str) -> ContextEntry | None: ...
    def entries(
        self, pool: Pool | None = None, *, project_id: str | None = None
    ) -> list[ContextEntry]: ...
    def search(
        self, query: str, *, pool: Pool | None = None, project_id: str | None = None
    ) -> list[ContextEntry]: ...
    def update(
        self, entry_id: str, *, text: str | None = None, tags: list[str] | None = None
    ) -> ContextEntry: ...
    def delete(self, entry_id: str) -> None: ...
    def promote(self, entry_id: str) -> ContextEntry: ...
    def ensure_project(self, project: ProjectInfo) -> None: ...
    def list_projects(self) -> list[ProjectInfo]: ...
    def get_project(self, project_id: str) -> ProjectInfo | None: ...
    def find_projects(self, term: str) -> list[ProjectInfo]: ...
    def add_linked_repo(self, project_id: str, repo: str) -> ProjectInfo: ...
    def add_prompt(
        self, text: str, project_id: str | None, source: str = "claude-code"
    ) -> PromptRecord: ...
    def recent_prompts(
        self, project_id: str | None = None, *, limit: int = 20
    ) -> list[PromptRecord]: ...
    def close(self) -> None: ...


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _row_to_entry(row: sqlite3.Row) -> ContextEntry:
    deleted_at = row["deleted_at"]
    return ContextEntry(
        id=row["id"],
        pool=row["pool"],
        project_id=row["project_id"],
        text=row["text"],
        tags=json.loads(row["tags"]),
        source=row["source"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        deleted_at=datetime.fromisoformat(deleted_at) if deleted_at else None,
    )


def _row_to_project(row: sqlite3.Row) -> ProjectInfo:
    return ProjectInfo(
        id=row["id"],
        root=Path(row["root"]),
        linked_repos=json.loads(row["linked_repos"]),
    )


def _row_to_prompt(row: sqlite3.Row) -> PromptRecord:
    return PromptRecord(
        id=row["id"],
        project_id=row["project_id"],
        text=row["text"],
        source=row["source"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _fts_match(query: str) -> str:
    """Turn free text into a safe FTS5 query: prefix-match every word token.

    Building the MATCH expression ourselves (rather than passing raw user input)
    keeps arbitrary punctuation from being parsed as FTS5 operators.
    """
    tokens = re.findall(r"[A-Za-z0-9]+", query)
    return " OR ".join(f"{token}*" for token in tokens)


def _scope_filter(
    pool: Pool | None, project_id: str | None, *, prefix: str = ""
) -> tuple[list[str], list[str]]:
    """Build the pool/project ``WHERE`` clauses and params for a query.

    With no ``pool`` but a ``project_id``, this is the in-scope view that
    unqualified ``entries``/``search`` show: the user pool plus that project's
    pool. ``prefix`` qualifies the column names for joined queries (e.g.
    ``"e."``).
    """
    pool_col, pid_col = f"{prefix}pool", f"{prefix}project_id"
    if pool == "user":
        return [f"{pool_col} = 'user'"], []
    if pool == "project":
        return [f"{pool_col} = 'project' AND {pid_col} = ?"], [project_id or ""]
    if project_id is not None:
        in_scope = f"({pool_col} = 'user' OR ({pool_col} = 'project' AND {pid_col} = ?))"
        return [in_scope], [project_id]
    return [f"{pool_col} = 'user'"], []


class SqliteStore:
    """The SQLite implementation of :class:`ContextStore`."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    def add(self, entry: ContextEntry) -> ContextEntry:
        self._conn.execute(
            f"INSERT INTO entry ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.id,
                entry.pool,
                entry.project_id,
                entry.text,
                json.dumps(entry.tags),
                entry.source,
                entry.created_at.isoformat(),
                entry.updated_at.isoformat(),
                entry.deleted_at.isoformat() if entry.deleted_at else None,
            ),
        )
        self._conn.commit()
        return entry

    def get(self, ref: str) -> ContextEntry | None:
        exact = self._conn.execute(
            f"SELECT {_COLUMNS} FROM entry WHERE id = ? AND deleted_at IS NULL", (ref,)
        ).fetchone()
        if exact is not None:
            return _row_to_entry(exact)
        matches = self._conn.execute(
            f"SELECT {_COLUMNS} FROM entry WHERE id GLOB ? AND deleted_at IS NULL LIMIT 2",
            (_glob_prefix(ref),),
        ).fetchall()
        if len(matches) > 1:
            raise AmbiguousIdError(ref)
        return _row_to_entry(matches[0]) if matches else None

    def entries(
        self, pool: Pool | None = None, *, project_id: str | None = None
    ) -> list[ContextEntry]:
        clauses, params = _scope_filter(pool, project_id)
        where = " AND ".join(["deleted_at IS NULL", *clauses])
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM entry WHERE {where} ORDER BY id", params
        ).fetchall()
        return [_row_to_entry(row) for row in rows]

    def search(
        self, query: str, *, pool: Pool | None = None, project_id: str | None = None
    ) -> list[ContextEntry]:
        match = _fts_match(query)
        if not match:
            return []
        clauses, params = _scope_filter(pool, project_id, prefix="e.")
        where = " AND ".join(["entry_fts MATCH ?", "e.deleted_at IS NULL", *clauses])
        columns = ", ".join(f"e.{col}" for col in _COLUMNS.split(", "))
        rows = self._conn.execute(
            f"SELECT {columns} FROM entry e JOIN entry_fts ON entry_fts.rowid = e.rowid "
            f"WHERE {where} ORDER BY rank",
            [match, *params],
        ).fetchall()
        return [_row_to_entry(row) for row in rows]

    def update(
        self, entry_id: str, *, text: str | None = None, tags: list[str] | None = None
    ) -> ContextEntry:
        entry = self.get(entry_id)
        if entry is None:
            raise KeyError(entry_id)
        self._conn.execute(
            "UPDATE entry SET text = ?, tags = ?, updated_at = ? WHERE id = ?",
            (
                entry.text if text is None else text,
                json.dumps(entry.tags if tags is None else tags),
                _now_iso(),
                entry.id,
            ),
        )
        self._conn.commit()
        updated = self.get(entry.id)
        assert updated is not None  # just updated, not deleted
        return updated

    def delete(self, entry_id: str) -> None:
        entry = self.get(entry_id)
        if entry is None:
            raise KeyError(entry_id)
        self._conn.execute("UPDATE entry SET deleted_at = ? WHERE id = ?", (_now_iso(), entry.id))
        self._conn.commit()

    def promote(self, entry_id: str) -> ContextEntry:
        entry = self.get(entry_id)
        if entry is None:
            raise KeyError(entry_id)
        if entry.pool != "project":
            raise ValueError(f"entry {entry.id} is already in the user pool")
        # Move in place: same id and history, now global. The CHECK constraint
        # requires project_id to be NULL for user-pool rows.
        self._conn.execute(
            "UPDATE entry SET pool = 'user', project_id = NULL, updated_at = ? WHERE id = ?",
            (_now_iso(), entry.id),
        )
        self._conn.commit()
        promoted = self.get(entry.id)
        assert promoted is not None  # just updated, not deleted
        return promoted

    def ensure_project(self, project: ProjectInfo) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO project (id, root, name, linked_repos, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                project.id,
                str(project.root),
                project.root.name or str(project.root),
                json.dumps(project.linked_repos),
                _now_iso(),
            ),
        )
        self._conn.commit()

    def list_projects(self) -> list[ProjectInfo]:
        rows = self._conn.execute(
            "SELECT id, root, linked_repos FROM project ORDER BY name"
        ).fetchall()
        return [_row_to_project(row) for row in rows]

    def get_project(self, project_id: str) -> ProjectInfo | None:
        row = self._conn.execute(
            "SELECT id, root, linked_repos FROM project WHERE id = ?", (project_id,)
        ).fetchone()
        return _row_to_project(row) if row is not None else None

    def find_projects(self, term: str) -> list[ProjectInfo]:
        rows = self._conn.execute(
            "SELECT id, root, linked_repos FROM project WHERE id GLOB ? OR name = ? ORDER BY name",
            (_glob_prefix(term), term),
        ).fetchall()
        return [_row_to_project(row) for row in rows]

    def add_linked_repo(self, project_id: str, repo: str) -> ProjectInfo:
        project = self.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        if repo not in project.linked_repos:
            self._conn.execute(
                "UPDATE project SET linked_repos = ? WHERE id = ?",
                (json.dumps([*project.linked_repos, repo]), project_id),
            )
            self._conn.commit()
        updated = self.get_project(project_id)
        assert updated is not None  # just confirmed it exists
        return updated

    def add_prompt(
        self, text: str, project_id: str | None, source: str = "claude-code"
    ) -> PromptRecord:
        record = PromptRecord(
            id=new_prompt_id(),
            project_id=project_id,
            text=text,
            source=source,
            created_at=datetime.now(tz=UTC),
        )
        self._conn.execute(
            f"INSERT INTO prompt ({_PROMPT_COLUMNS}) VALUES (?, ?, ?, ?, ?)",
            (
                record.id,
                record.project_id,
                record.text,
                record.source,
                record.created_at.isoformat(),
            ),
        )
        self._conn.commit()
        return record

    def recent_prompts(
        self, project_id: str | None = None, *, limit: int = 20
    ) -> list[PromptRecord]:
        if project_id is None:
            rows = self._conn.execute(
                f"SELECT {_PROMPT_COLUMNS} FROM prompt ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"SELECT {_PROMPT_COLUMNS} FROM prompt "
                "WHERE project_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return [_row_to_prompt(row) for row in rows]

    def close(self) -> None:
        self._conn.close()


def _glob_prefix(ref: str) -> str:
    """Escape GLOB metacharacters in ``ref`` and append a wildcard."""
    escaped = ref.translate({ord("*"): None, ord("?"): None, ord("["): None})
    return f"{escaped}*"


def _migrate(connection: sqlite3.Connection) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    for index in range(version, len(_MIGRATIONS)):
        connection.executescript(_MIGRATIONS[index])
        connection.execute(f"PRAGMA user_version = {index + 1}")
    connection.commit()


def open_store() -> ContextStore:
    """Open (creating and migrating if needed) the context store."""
    paths.ensure_home()
    connection = sqlite3.connect(str(paths.db_path()))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA foreign_keys = ON")
    _migrate(connection)
    return SqliteStore(connection)


@contextmanager
def store_session() -> Iterator[ContextStore]:
    """Open a store for the duration of a ``with`` block and close it after."""
    store = open_store()
    try:
        yield store
    finally:
        store.close()
