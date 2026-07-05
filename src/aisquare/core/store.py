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
from aisquare.models import (
    ContextEntry,
    Pool,
    ProjectInfo,
    PromptRecord,
    TaskStatus,
    TeamEvent,
    TeamSession,
    TeamTask,
)

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

# v3: the team bus — sessions, shared tasks and the event stream that give
# parallel agent sessions working memory of each other. ``team_event.seq`` is
# the AUTOINCREMENT bus cursor: deltas are "rows past my cursor, by others".
_SCHEMA_V3 = """
CREATE TABLE team_session (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'unassigned',
    label         TEXT,
    focus         TEXT,
    started_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    ended_at      TEXT,
    cursor        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX team_session_project ON team_session (project_id, last_seen_at);

CREATE TABLE team_task (
    id                TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL,
    key               TEXT NOT NULL,
    title             TEXT NOT NULL,
    detail            TEXT,
    status            TEXT NOT NULL DEFAULT 'todo'
        CHECK (status IN ('todo', 'doing', 'blocked', 'done', 'dropped')),
    role              TEXT,
    claimed_by        TEXT,
    claim_expires_at  TEXT,
    created_by        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (project_id, key)
);
CREATE INDEX team_task_project_status ON team_task (project_id, status);

CREATE TABLE team_event (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    id          TEXT NOT NULL UNIQUE,
    project_id  TEXT NOT NULL,
    session_id  TEXT,
    kind        TEXT NOT NULL,
    text        TEXT NOT NULL,
    task_id     TEXT,
    to_role     TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX team_event_project_seq ON team_event (project_id, seq);
"""

# Ordered migrations; index i upgrades the db from user_version i to i+1.
_MIGRATIONS = (_SCHEMA_V1, _SCHEMA_V2, _SCHEMA_V3)
SCHEMA_VERSION = len(_MIGRATIONS)

_COLUMNS = "id, pool, project_id, text, tags, source, created_at, updated_at, deleted_at"
_PROMPT_COLUMNS = "id, project_id, text, source, created_at"
_SESSION_COLUMNS = "id, project_id, role, label, focus, started_at, last_seen_at, ended_at, cursor"
_TASK_COLUMNS = (
    "id, project_id, key, title, detail, status, role, "
    "claimed_by, claim_expires_at, created_by, created_at, updated_at"
)
_EVENT_COLUMNS = "seq, id, project_id, session_id, kind, text, task_id, to_role, created_at"


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
    def team_active(self, project_id: str) -> bool: ...
    def upsert_session(self, session: TeamSession) -> TeamSession: ...
    def get_session(self, session_id: str) -> TeamSession | None: ...
    def team_sessions(self, project_id: str) -> list[TeamSession]: ...
    def update_session(
        self,
        session_id: str,
        *,
        role: str | None = None,
        label: str | None = None,
        focus: str | None = None,
    ) -> TeamSession: ...
    def touch_session(self, session_id: str, *, cursor: int | None = None) -> None: ...
    def end_session(self, session_id: str) -> list[TeamTask]: ...
    def upsert_task(self, task: TeamTask) -> tuple[TeamTask, bool]: ...
    def get_task(self, ref: str) -> TeamTask | None: ...
    def team_tasks(
        self, project_id: str, *, status: TaskStatus | None = None
    ) -> list[TeamTask]: ...
    def claim_task(self, task_id: str, session_ref: str, lease_until: datetime) -> bool: ...
    def renew_leases(self, session_id: str, lease_until: datetime) -> None: ...
    def set_task_status(self, task_id: str, status: TaskStatus) -> TeamTask: ...
    def release_task(self, task_id: str) -> TeamTask: ...
    def add_team_event(self, event: TeamEvent) -> TeamEvent: ...
    def events_since(
        self, project_id: str, seq: int, *, exclude_session: str | None = None, limit: int = 50
    ) -> list[TeamEvent]: ...
    def recent_events(self, project_id: str, *, limit: int = 10) -> list[TeamEvent]: ...
    def latest_seq(self, project_id: str) -> int: ...
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


def _maybe_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _row_to_session(row: sqlite3.Row) -> TeamSession:
    return TeamSession(
        id=row["id"],
        project_id=row["project_id"],
        role=row["role"],
        label=row["label"],
        focus=row["focus"],
        started_at=datetime.fromisoformat(row["started_at"]),
        last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
        ended_at=_maybe_dt(row["ended_at"]),
        cursor=row["cursor"],
    )


def _row_to_task(row: sqlite3.Row) -> TeamTask:
    return TeamTask(
        id=row["id"],
        project_id=row["project_id"],
        key=row["key"],
        title=row["title"],
        detail=row["detail"],
        status=row["status"],
        role=row["role"],
        claimed_by=row["claimed_by"],
        claim_expires_at=_maybe_dt(row["claim_expires_at"]),
        created_by=row["created_by"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _row_to_event(row: sqlite3.Row) -> TeamEvent:
    return TeamEvent(
        seq=row["seq"],
        id=row["id"],
        project_id=row["project_id"],
        session_id=row["session_id"],
        kind=row["kind"],
        text=row["text"],
        task_id=row["task_id"],
        to_role=row["to_role"],
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

    # --- team bus ------------------------------------------------------------

    def team_active(self, project_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM team_session WHERE project_id = ? "
            "UNION SELECT 1 FROM team_task WHERE project_id = ? "
            "UNION SELECT 1 FROM team_event WHERE project_id = ? LIMIT 1",
            (project_id, project_id, project_id),
        ).fetchone()
        return row is not None

    def upsert_session(self, session: TeamSession) -> TeamSession:
        """Insert the session, or revive/refresh it if the id is already known."""
        self._conn.execute(
            f"INSERT INTO team_session ({_SESSION_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET "
            "last_seen_at = excluded.last_seen_at, ended_at = NULL",
            (
                session.id,
                session.project_id,
                session.role,
                session.label,
                session.focus,
                session.started_at.isoformat(),
                session.last_seen_at.isoformat(),
                session.ended_at.isoformat() if session.ended_at else None,
                session.cursor,
            ),
        )
        self._conn.commit()
        stored = self.get_session(session.id)
        assert stored is not None  # just upserted
        return stored

    def get_session(self, session_id: str) -> TeamSession | None:
        row = self._conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM team_session WHERE id = ?", (session_id,)
        ).fetchone()
        if row is not None:
            return _row_to_session(row)
        matches = self._conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM team_session WHERE id GLOB ? LIMIT 2",
            (_glob_prefix(session_id),),
        ).fetchall()
        if len(matches) > 1:
            raise AmbiguousIdError(session_id)
        return _row_to_session(matches[0]) if matches else None

    def team_sessions(self, project_id: str) -> list[TeamSession]:
        rows = self._conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM team_session "
            "WHERE project_id = ? ORDER BY last_seen_at DESC",
            (project_id,),
        ).fetchall()
        return [_row_to_session(row) for row in rows]

    def update_session(
        self,
        session_id: str,
        *,
        role: str | None = None,
        label: str | None = None,
        focus: str | None = None,
    ) -> TeamSession:
        session = self.get_session(session_id)
        if session is None:
            raise KeyError(session_id)
        self._conn.execute(
            "UPDATE team_session SET role = ?, label = ?, focus = ?, last_seen_at = ? WHERE id = ?",
            (
                role if role is not None else session.role,
                label if label is not None else session.label,
                focus if focus is not None else session.focus,
                _now_iso(),
                session.id,
            ),
        )
        self._conn.commit()
        updated = self.get_session(session.id)
        assert updated is not None  # just updated
        return updated

    def touch_session(self, session_id: str, *, cursor: int | None = None) -> None:
        """Heartbeat: bump ``last_seen_at`` (and advance the delta cursor)."""
        if cursor is None:
            self._conn.execute(
                "UPDATE team_session SET last_seen_at = ? WHERE id = ?",
                (_now_iso(), session_id),
            )
        else:
            self._conn.execute(
                "UPDATE team_session SET last_seen_at = ?, cursor = ? WHERE id = ?",
                (_now_iso(), cursor, session_id),
            )
        self._conn.commit()

    def end_session(self, session_id: str) -> list[TeamTask]:
        """Mark the session ended and release its claims; returns released tasks."""
        released = [
            _row_to_task(row)
            for row in self._conn.execute(
                f"SELECT {_TASK_COLUMNS} FROM team_task WHERE claimed_by = ? AND status = 'doing'",
                (session_id,),
            ).fetchall()
        ]
        now = _now_iso()
        self._conn.execute(
            "UPDATE team_task SET status = 'todo', claimed_by = NULL, "
            "claim_expires_at = NULL, updated_at = ? WHERE claimed_by = ? AND status = 'doing'",
            (now, session_id),
        )
        self._conn.execute(
            "UPDATE team_session SET ended_at = ?, last_seen_at = ? WHERE id = ?",
            (now, now, session_id),
        )
        self._conn.commit()
        return released

    def upsert_task(self, task: TeamTask) -> tuple[TeamTask, bool]:
        """Add a task; a duplicate ``(project_id, key)`` returns the existing one.

        The second element reports whether a new task was created — the
        idempotency contract for ``task add``.
        """
        cursor = self._conn.execute(
            f"INSERT INTO team_task ({_TASK_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (project_id, key) DO NOTHING",
            (
                task.id,
                task.project_id,
                task.key,
                task.title,
                task.detail,
                task.status,
                task.role,
                task.claimed_by,
                task.claim_expires_at.isoformat() if task.claim_expires_at else None,
                task.created_by,
                task.created_at.isoformat(),
                task.updated_at.isoformat(),
            ),
        )
        created = cursor.rowcount == 1
        self._conn.commit()
        row = self._conn.execute(
            f"SELECT {_TASK_COLUMNS} FROM team_task WHERE project_id = ? AND key = ?",
            (task.project_id, task.key),
        ).fetchone()
        assert row is not None  # inserted or already present
        return _row_to_task(row), created

    def get_task(self, ref: str) -> TeamTask | None:
        row = self._conn.execute(
            f"SELECT {_TASK_COLUMNS} FROM team_task WHERE id = ?", (ref,)
        ).fetchone()
        if row is not None:
            return _row_to_task(row)
        matches = self._conn.execute(
            f"SELECT {_TASK_COLUMNS} FROM team_task WHERE id GLOB ? LIMIT 2",
            (_glob_prefix(ref),),
        ).fetchall()
        if len(matches) > 1:
            raise AmbiguousIdError(ref)
        return _row_to_task(matches[0]) if matches else None

    def team_tasks(self, project_id: str, *, status: TaskStatus | None = None) -> list[TeamTask]:
        if status is None:
            rows = self._conn.execute(
                f"SELECT {_TASK_COLUMNS} FROM team_task WHERE project_id = ? ORDER BY id",
                (project_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"SELECT {_TASK_COLUMNS} FROM team_task "
                "WHERE project_id = ? AND status = ? ORDER BY id",
                (project_id, status),
            ).fetchall()
        return [_row_to_task(row) for row in rows]

    def claim_task(self, task_id: str, session_ref: str, lease_until: datetime) -> bool:
        """Atomically claim a task; exactly one concurrent claimer wins.

        Claimable: open (``todo``/``blocked``), or ``doing`` with an expired
        lease (the previous claimant is presumed gone).
        """
        cursor = self._conn.execute(
            "UPDATE team_task SET status = 'doing', claimed_by = ?, "
            "claim_expires_at = ?, updated_at = ? "
            "WHERE id = ? AND (status IN ('todo', 'blocked') "
            "OR (status = 'doing' AND claim_expires_at < ?))",
            (session_ref, lease_until.isoformat(), _now_iso(), task_id, _now_iso()),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def renew_leases(self, session_id: str, lease_until: datetime) -> None:
        """Extend the claim lease on everything this session is working on."""
        self._conn.execute(
            "UPDATE team_task SET claim_expires_at = ? WHERE claimed_by = ? AND status = 'doing'",
            (lease_until.isoformat(), session_id),
        )
        self._conn.commit()

    def set_task_status(self, task_id: str, status: TaskStatus) -> TeamTask:
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        self._conn.execute(
            "UPDATE team_task SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now_iso(), task.id),
        )
        self._conn.commit()
        updated = self.get_task(task.id)
        assert updated is not None  # just updated
        return updated

    def release_task(self, task_id: str) -> TeamTask:
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        self._conn.execute(
            "UPDATE team_task SET status = 'todo', claimed_by = NULL, "
            "claim_expires_at = NULL, updated_at = ? WHERE id = ?",
            (_now_iso(), task.id),
        )
        self._conn.commit()
        updated = self.get_task(task.id)
        assert updated is not None  # just updated
        return updated

    def add_team_event(self, event: TeamEvent) -> TeamEvent:
        cursor = self._conn.execute(
            "INSERT INTO team_event (id, project_id, session_id, kind, text, "
            "task_id, to_role, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.id,
                event.project_id,
                event.session_id,
                event.kind,
                event.text,
                event.task_id,
                event.to_role,
                event.created_at.isoformat(),
            ),
        )
        self._conn.commit()
        return event.model_copy(update={"seq": cursor.lastrowid})

    def events_since(
        self, project_id: str, seq: int, *, exclude_session: str | None = None, limit: int = 50
    ) -> list[TeamEvent]:
        rows = self._conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM team_event "
            "WHERE project_id = ? AND seq > ? "
            "AND (session_id IS NULL OR session_id != ?) "
            "ORDER BY seq LIMIT ?",
            (project_id, seq, exclude_session or "", limit),
        ).fetchall()
        return [_row_to_event(row) for row in rows]

    def recent_events(self, project_id: str, *, limit: int = 10) -> list[TeamEvent]:
        rows = self._conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM team_event "
            "WHERE project_id = ? ORDER BY seq DESC LIMIT ?",
            (project_id, limit),
        ).fetchall()
        return [_row_to_event(row) for row in reversed(rows)]

    def latest_seq(self, project_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM team_event WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return int(row[0])

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
