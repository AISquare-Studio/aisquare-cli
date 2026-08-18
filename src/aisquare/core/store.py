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

import contextlib
import json
import os
import random
import re
import sqlite3
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

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

# v3: the orchestrator — sessions, shared tasks and the event stream that give
# parallel agent sessions working memory of each other. ``team_event.seq`` is
# the AUTOINCREMENT stream cursor: deltas are "rows past my cursor, by others".
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

# v4: the review stage of the task lifecycle (coder → review → runner verifies)
# — a CHECK constraint cannot be altered in SQLite, so the table is rebuilt —
# and team_meta, small durable key/values (e.g. the distiller's watermark).
_SCHEMA_V4 = """
DROP INDEX team_task_project_status;
ALTER TABLE team_task RENAME TO team_task_v3;
CREATE TABLE team_task (
    id                TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL,
    key               TEXT NOT NULL,
    title             TEXT NOT NULL,
    detail            TEXT,
    status            TEXT NOT NULL DEFAULT 'todo'
        CHECK (status IN ('todo', 'doing', 'review', 'blocked', 'done', 'dropped')),
    role              TEXT,
    claimed_by        TEXT,
    claim_expires_at  TEXT,
    created_by        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (project_id, key)
);
INSERT INTO team_task SELECT * FROM team_task_v3;
DROP TABLE team_task_v3;
CREATE INDEX team_task_project_status ON team_task (project_id, status);

CREATE TABLE team_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""

# v5: task dependencies — a JSON array of task ids; a todo task is "ready"
# (and eligible for `task next`) only when everything it needs is resolved.
_SCHEMA_V5 = """
ALTER TABLE team_task ADD COLUMN needs TEXT NOT NULL DEFAULT '[]';
"""

# v6: live session state — working (mid-turn), waiting (turn ended, wants
# input) or attention (permission request / idle notice) — driven by the
# UserPromptSubmit / Stop / Notification hooks.
_SCHEMA_V6 = """
ALTER TABLE team_session ADD COLUMN state TEXT NOT NULL DEFAULT 'working';
"""

# v7: where each session's Claude Code transcript lives (from hook payloads) —
# lets the board jump from a task/event to the conversation that produced it.
_SCHEMA_V7 = """
ALTER TABLE team_session ADD COLUMN transcript_path TEXT;
"""

# v8: retire pre-release single-colon MCP virtual sessions (mcp:<client>);
# the per-project format (mcp:<client>:<proj>) replaced them and the old rows
# would otherwise linger as phantom live sessions on the first project served.
_SCHEMA_V8 = """
DELETE FROM team_session WHERE id LIKE 'mcp:%' AND substr(id, 5) NOT LIKE '%:%';
"""

# v9: which agent config dir (account) a session runs under, so a board driven
# by several parallel installs shows who is on which — and a rate-limited
# account's sessions can be spotted and relaunched elsewhere.
_SCHEMA_V9 = """
ALTER TABLE team_session ADD COLUMN account TEXT;
"""

# v10: the harness captures which model (and effort) each session actually
# runs on — from the SessionStart hook payload, where both fields are optional
# — so the board can flag a session whose model falls outside its role's
# ladder. (v10, not v9 as authored on the PR branch: the train's v9 is the
# account column above, and a db that ran either migration must still get the
# other's columns.)
_SCHEMA_V10 = """
ALTER TABLE team_session ADD COLUMN model TEXT;
ALTER TABLE team_session ADD COLUMN effort TEXT;
"""

# Ordered migrations; index i upgrades the db from user_version i to i+1.
_MIGRATIONS = (
    _SCHEMA_V1,
    _SCHEMA_V2,
    _SCHEMA_V3,
    _SCHEMA_V4,
    _SCHEMA_V5,
    _SCHEMA_V6,
    _SCHEMA_V7,
    _SCHEMA_V8,
    _SCHEMA_V9,
    _SCHEMA_V10,
)
SCHEMA_VERSION = len(_MIGRATIONS)

_COLUMNS = "id, pool, project_id, text, tags, source, created_at, updated_at, deleted_at"
_PROMPT_COLUMNS = "id, project_id, text, source, created_at"
_SESSION_COLUMNS = (
    "id, project_id, role, label, focus, started_at, last_seen_at, ended_at, cursor, state, "
    "transcript_path, account, model, effort"
)
_TASK_COLUMNS = (
    "id, project_id, key, title, detail, status, role, needs, "
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
    def touch_session(
        self, session_id: str, *, cursor: int | None = None, state: str | None = None
    ) -> None: ...
    def mark_attention(self, session_id: str) -> bool: ...
    def end_session(self, session_id: str, *, release_claims: bool = True) -> list[TeamTask]: ...
    def upsert_task(self, task: TeamTask) -> tuple[TeamTask, bool]: ...
    def get_task(self, ref: str) -> TeamTask | None: ...
    def team_tasks(
        self, project_id: str, *, status: TaskStatus | None = None
    ) -> list[TeamTask]: ...
    def claim_task(self, task_id: str, session_ref: str, lease_until: datetime) -> bool: ...
    def renew_leases(self, session_id: str, lease_until: datetime) -> None: ...
    def set_task_status(self, task_id: str, status: TaskStatus) -> TeamTask: ...
    def release_task(self, task_id: str) -> TeamTask: ...
    def reopen_task(self, task_id: str) -> TeamTask: ...
    def next_task(
        self, project_id: str, *, role: str | None = None, status: TaskStatus = "todo"
    ) -> TeamTask | None: ...
    def get_meta(self, key: str) -> str | None: ...
    def set_meta(self, key: str, value: str) -> None: ...
    def list_meta(self, prefix: str) -> dict[str, str]: ...
    def add_signal_event(
        self, event: TeamEvent, meta_key: str, meta_value: dict[str, Any]
    ) -> TeamEvent: ...
    def add_team_event(self, event: TeamEvent) -> TeamEvent: ...
    def get_event(self, event_id: str) -> TeamEvent | None: ...
    def get_event_by_seq(self, seq: int) -> TeamEvent | None: ...
    def find_event_by_id(self, ref: str) -> TeamEvent | None: ...
    def events_since(
        self, project_id: str, seq: int, *, exclude_session: str | None = None, limit: int = 50
    ) -> list[TeamEvent]: ...
    def recent_events(self, project_id: str, *, limit: int = 10) -> list[TeamEvent]: ...
    def filtered_events(
        self,
        project_id: str,
        *,
        session_id: str | None = None,
        since_iso: str | None = None,
        since_seq: int | None = None,
        kind: str | None = None,
        task_id: str | None = None,
        limit: int = 30,
    ) -> list[TeamEvent]: ...
    def latest_seq(self, project_id: str) -> int: ...
    def terminal_events(self, project_id: str) -> dict[str, TeamEvent]: ...
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
        state=row["state"],
        transcript_path=row["transcript_path"],
        account=row["account"],
        model=row["model"],
        effort=row["effort"],
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
        needs=json.loads(row["needs"]),
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


def unmet_needs(task: TeamTask, statuses: Mapping[str, str]) -> list[str]:
    """The dependencies of ``task`` that are not resolved yet.

    A need is satisfied once its task is ``done`` or ``dropped``; anything
    else (including an unknown id) keeps the dependent task waiting.
    """
    return [need for need in task.needs if statuses.get(need) not in ("done", "dropped")]


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

    # --- orchestrator ------------------------------------------------------------

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
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET "
            "last_seen_at = excluded.last_seen_at, ended_at = NULL, "
            "state = 'working', "
            "transcript_path = COALESCE(excluded.transcript_path, transcript_path), "
            "account = COALESCE(excluded.account, account), "
            "model = COALESCE(excluded.model, model), "
            "effort = COALESCE(excluded.effort, effort)",
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
                session.state,
                session.transcript_path,
                session.account,
                session.model,
                session.effort,
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

    def touch_session(
        self, session_id: str, *, cursor: int | None = None, state: str | None = None
    ) -> None:
        """Heartbeat: bump ``last_seen_at``, un-retire the row, advance cursor/state.

        ``ended_at = NULL`` is the repair :meth:`end_session` already promises in
        its own docstring, and until #47 nothing performed it: ``upsert_session``
        clears the field, but it only runs at ``SessionStart``, while every
        subsequent proof of life arrives here. A session retired on a cadence
        artifact therefore kept working — notes delivered with verifiable
        receipts, roles set, claims held — while being invisible to ``board``,
        ``team status``, ``watch`` and ``doctor``, every one of which reads
        liveness as ``ended_at IS NULL``. Operators read row-absence as death.

        A heartbeat is EVIDENCE; prune's retirement was an inference from
        silence. The evidence wins. Nothing resurrects on its own — only a
        signal from the session itself reaches this method.
        """
        sets, params = ["last_seen_at = ?", "ended_at = NULL"], [_now_iso()]
        if cursor is not None:
            sets.append("cursor = ?")
            params.append(str(cursor))
        if state is not None:
            sets.append("state = ?")
            params.append(state)
        self._conn.execute(
            f"UPDATE team_session SET {', '.join(sets)} WHERE id = ?",
            (*params, session_id),
        )
        self._conn.commit()

    def mark_attention(self, session_id: str) -> bool:
        """Flip a session into the attention state, atomically.

        Returns True only for the transition — concurrent Notification hooks
        (parallel permission prompts happen) must produce exactly one feed
        event, so the read and the write are one conditional UPDATE.

        The unconditional second statement carries the un-retirement (#47), not
        the first: a session already parked in ``attention`` must still have a
        wrongly retired row repaired, and the first statement deliberately does
        not match it. A session waiting on a permission prompt is the most alive
        it ever is, and the one a human is most likely hunting for on the board.
        """
        cursor = self._conn.execute(
            "UPDATE team_session SET state = 'attention', last_seen_at = ? "
            "WHERE id = ? AND state <> 'attention'",
            (_now_iso(), session_id),
        )
        self._conn.execute(
            "UPDATE team_session SET last_seen_at = ?, ended_at = NULL WHERE id = ?",
            (_now_iso(), session_id),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def end_session(self, session_id: str, *, release_claims: bool = True) -> list[TeamTask]:
        """Mark the session ended; optionally release its claims.

        ``release_claims=False`` retires only the session's PRESENCE and leaves
        its ``doing`` tasks claimed. The two effects are separable because they
        have very different costs when the caller is wrong: a wrongly retired
        presence row is repaired by the session's next heartbeat, while a
        wrongly released claim hands live work to a second agent and nothing
        repairs it. Callers that cannot PROVE the session is dead pass False.
        The return value still lists the tasks that WOULD have been released,
        so the caller can report them either way.
        """
        released = [
            _row_to_task(row)
            for row in self._conn.execute(
                f"SELECT {_TASK_COLUMNS} FROM team_task WHERE claimed_by = ? AND status = 'doing'",
                (session_id,),
            ).fetchall()
        ]
        now = _now_iso()
        if release_claims:
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
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (project_id, key) DO NOTHING",
            (
                task.id,
                task.project_id,
                task.key,
                task.title,
                task.detail,
                task.status,
                task.role,
                json.dumps(task.needs),
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
        if matches:
            return _row_to_task(matches[0])
        # Boards display the id *tail* (the unique random part), so tails
        # must resolve too: fall back to a suffix match.
        matches = self._conn.execute(
            f"SELECT {_TASK_COLUMNS} FROM team_task WHERE id GLOB ? LIMIT 2",
            (f"*{_glob_prefix(ref)[:-1]}",),
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
        if status in ("done", "dropped"):
            # Terminal: clear the claim so the task never *looks* held —
            # a lingering claimed_by invites a bogus "release" back to todo.
            self._conn.execute(
                "UPDATE team_task SET status = ?, claimed_by = NULL, "
                "claim_expires_at = NULL, updated_at = ? WHERE id = ?",
                (status, _now_iso(), task.id),
            )
        else:
            self._conn.execute(
                "UPDATE team_task SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now_iso(), task.id),
            )
        self._conn.commit()
        updated = self.get_task(task.id)
        assert updated is not None  # just updated
        return updated

    def release_task(self, task_id: str) -> TeamTask:
        """Give a claimed (``doing``) task back to the pool.

        Guarded so a "release" can never resurrect a finished task: raises
        ``ValueError`` unless the task is currently ``doing``.
        """
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        cursor = self._conn.execute(
            "UPDATE team_task SET status = 'todo', claimed_by = NULL, "
            "claim_expires_at = NULL, updated_at = ? WHERE id = ? AND status = 'doing'",
            (_now_iso(), task.id),
        )
        self._conn.commit()
        if cursor.rowcount != 1:
            raise ValueError(f"task {task.id} is {task.status}, not doing — nothing to release")
        updated = self.get_task(task.id)
        assert updated is not None  # just updated
        return updated

    def reopen_task(self, task_id: str) -> TeamTask:
        """Send a non-todo task back to the pool (verification failed, etc.).

        Unlike ``release_task`` this is the deliberate resurrection path —
        it accepts ``doing``/``review``/``blocked``/``done`` (but not
        ``dropped``: discarded work needs an explicit new task).
        """
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        cursor = self._conn.execute(
            "UPDATE team_task SET status = 'todo', claimed_by = NULL, "
            "claim_expires_at = NULL, updated_at = ? "
            "WHERE id = ? AND status IN ('doing', 'review', 'blocked', 'done')",
            (_now_iso(), task.id),
        )
        self._conn.commit()
        if cursor.rowcount != 1:
            raise ValueError(
                f"task {task.id} is {task.status} — reopen a doing/review/blocked/done task"
            )
        updated = self.get_task(task.id)
        assert updated is not None  # just updated
        return updated

    def next_task(
        self, project_id: str, *, role: str | None = None, status: TaskStatus = "todo"
    ) -> TeamTask | None:
        """The oldest *ready* task in ``status`` a session of ``role`` could pick up.

        Tasks without a role hint are available to every role; tasks with one
        only match sessions of that role (or an unfiltered query). A ``todo``
        task is ready only when every task it needs is resolved — so loopers
        never receive work whose prerequisites are still in flight.
        """
        clauses = ["project_id = ?", "status = ?"]
        params: list[str] = [project_id, status]
        if role is not None:
            clauses.append("(role IS NULL OR role = ?)")
            params.append(role)
        rows = self._conn.execute(
            f"SELECT {_TASK_COLUMNS} FROM team_task WHERE {' AND '.join(clauses)} ORDER BY id",
            params,
        ).fetchall()
        if not rows:
            return None
        statuses = self.task_statuses(project_id)
        for row in rows:
            task = _row_to_task(row)
            if status != "todo" or not unmet_needs(task, statuses):
                return task
        return None

    def task_statuses(self, project_id: str) -> dict[str, str]:
        """Every task's status, for dependency-readiness checks and rendering."""
        rows = self._conn.execute(
            "SELECT id, status FROM team_task WHERE project_id = ?", (project_id,)
        ).fetchall()
        return {row["id"]: row["status"] for row in rows}

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM team_meta WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row is not None else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO team_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def list_meta(self, prefix: str) -> dict[str, str]:
        """Every ``team_meta`` entry under ``prefix``, key → value."""
        rows = self._conn.execute(
            "SELECT key, value FROM team_meta WHERE key GLOB ? ORDER BY key",
            (f"{_glob_prefix(prefix)[:-1]}*",),
        ).fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def add_signal_event(
        self, event: TeamEvent, meta_key: str, meta_value: dict[str, Any]
    ) -> TeamEvent:
        """Insert a signal event AND its meta materialization in ONE transaction.

        A signal is two writes — the pipe event (history) and the current-state
        blob under ``meta_key`` — and a crash between separate commits would
        leave watchers and readers disagreeing. The event's assigned ``seq``
        is injected into ``meta_value`` before the blob is stored, so the
        state row always names the event that produced it.
        """
        with self._conn:  # one BEGIN…COMMIT for both statements
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
            stamped = {**meta_value, "seq": cursor.lastrowid}
            self._conn.execute(
                "INSERT INTO team_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (meta_key, json.dumps(stamped)),
            )
        return event.model_copy(update={"seq": cursor.lastrowid})

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

    def get_event(self, event_id: str) -> TeamEvent | None:
        """One event by exact id — the write path's post-commit read-back."""
        row = self._conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM team_event WHERE id = ?", (event_id,)
        ).fetchone()
        return _row_to_event(row) if row is not None else None

    def get_event_by_seq(self, seq: int) -> TeamEvent | None:
        """One event by stream position (``seq`` is unique across all boards)."""
        row = self._conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM team_event WHERE seq = ?", (seq,)
        ).fetchone()
        return _row_to_event(row) if row is not None else None

    def find_event_by_id(self, ref: str) -> TeamEvent | None:
        """One event by id, prefix ok — receipts quote either form.

        Board-agnostic on purpose: ``team verify`` decides whether the match
        belongs to the caller's board (and hints where it actually lives).
        """
        exact = self.get_event(ref)
        if exact is not None:
            return exact
        matches = self._conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM team_event WHERE id GLOB ? LIMIT 2",
            (_glob_prefix(ref),),
        ).fetchall()
        if len(matches) > 1:
            raise AmbiguousIdError(ref)
        return _row_to_event(matches[0]) if matches else None

    def filtered_events(
        self,
        project_id: str,
        *,
        session_id: str | None = None,
        since_iso: str | None = None,
        since_seq: int | None = None,
        kind: str | None = None,
        task_id: str | None = None,
        limit: int = 30,
    ) -> list[TeamEvent]:
        """Matching events for ``team log``'s filters, oldest-first.

        Without ``since_seq`` this is a window: the NEWEST ``limit`` matches.
        With ``since_seq`` it is a cursor page: the OLDEST ``limit`` matches
        past that seq, so a client tracking the last seq it saw never skips
        events (the MCP ``team_log`` contract). Rides the ``(project_id,
        seq)`` index; ``since_iso`` compares stored ISO-8601 UTC strings
        lexicographically (uniform format by construction). ``session_id`` is
        exact — prefix resolution is the service layer's job.
        """
        clauses = ["project_id = ?"]
        params: list[str | int] = [project_id]
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if since_iso is not None:
            clauses.append("created_at >= ?")
            params.append(since_iso)
        if since_seq is not None:
            clauses.append("seq > ?")
            params.append(since_seq)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        paging = since_seq is not None
        rows = self._conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM team_event "
            f"WHERE {' AND '.join(clauses)} ORDER BY seq {'ASC' if paging else 'DESC'} LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [_row_to_event(row) for row in (rows if paging else reversed(rows))]

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

    def terminal_events(self, project_id: str) -> dict[str, TeamEvent]:
        """The latest done/dropped event per task — archive attribution.

        Queried from the store (not a bounded feed cache) so a task closed
        thousands of events ago still knows who closed it and when.
        """
        rows = self._conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM team_event WHERE seq IN ("
            "  SELECT MAX(seq) FROM team_event "
            "  WHERE project_id = ? AND task_id IS NOT NULL "
            "  AND kind IN ('task_done', 'task_dropped') GROUP BY task_id"
            ")",
            (project_id,),
        ).fetchall()
        events = [_row_to_event(row) for row in rows]
        return {event.task_id: event for event in events if event.task_id is not None}

    def close(self) -> None:
        self._conn.close()


def _glob_prefix(ref: str) -> str:
    """Escape GLOB metacharacters in ``ref`` and append a wildcard."""
    escaped = ref.translate({ord("*"): None, ord("?"): None, ord("["): None})
    return f"{escaped}*"


_DEFAULT_BUSY_MS = 5000
_MAX_BUSY_MS = 2**31 - 1  # SQLite's 32-bit atoi wraps anything larger to 0


def _busy_timeout_ms() -> int:
    """How long a connection waits on a locked store (``AISQUARE_DB_BUSY_MS``).

    Tests wedge the store on purpose and must not sit through the 5s default;
    anything unset, non-numeric or negative falls back to it. Values past
    2**31-1 clamp — SQLite parses the pragma with 32-bit atoi, so 2147483648
    would wrap to 0 and silently DISABLE the busy handler.
    """
    raw = os.environ.get("AISQUARE_DB_BUSY_MS", "")
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_BUSY_MS
    if value < 0:
        return _DEFAULT_BUSY_MS
    return min(value, _MAX_BUSY_MS)


def is_locked_error(exc: sqlite3.Error) -> bool:
    """True for transient lock/busy conditions worth a short retry.

    Everything else an ``sqlite3.DatabaseError`` carries — no such table,
    readonly database, disk I/O, corruption — is NOT retryable and must not
    be dressed up as ``store_locked``.
    """
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    # Only errors raised BY the sqlite3 module carry sqlite_errorcode;
    # hand-constructed ones (tests, wrappers) fall back to the message text.
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def is_corrupt_error(exc: sqlite3.Error) -> bool:
    """True when SQLite says the FILE is damaged, whenever it noticed.

    Needed at query time, where nothing has raised ``StoreUnopenable`` because
    nothing failed to open: a zeroed page with an intact header opens fine and
    a ``SELECT`` finds it later. So the decision has to be made from the error
    rather than from where it came from.

    Narrow, and the narrowness is the point. ``OperationalError`` IS a
    ``DatabaseError``, so a widening keyed on the base class would sweep up
    "database is locked" — transient contention, which five sessions hit
    nightly — and tell those operators their board is damaged. It would also
    sweep up "no such table", which is a defect in OUR migrations and must keep
    its traceback. Errorcode first, message only as the fallback for
    hand-constructed exceptions, exactly as ``is_locked_error`` does.

    This helper existed, was deleted as dead code when ``StoreUnopenable``
    replaced its only caller, and is back because the query-time seam cannot be
    written without it.
    """
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xFF in (sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_CORRUPT)
    text = str(exc).lower()
    return "not a database" in text or "disk image is malformed" in text


class StoreUnopenable(sqlite3.DatabaseError):
    """The store could not be opened at all — whatever the cause.

    Subclasses ``sqlite3.DatabaseError`` deliberately: every existing catcher
    (``_fail_team``'s ``STORE_ERRORS``, diagnostics' bare ``except``) keeps
    working unchanged, while the one boundary that wants to translate it can
    name it precisely.

    Raised ONLY from ``open_store``'s setup phase, which is what makes it safe
    to act on: at that point nothing in the file is reachable by this CLI, so
    recommending the operator move it aside costs them no history they still
    had. A ``DatabaseError`` from a later query is a different animal and is
    left alone — it may be a bug in our SQL against a perfectly good store.

    A lock timeout is NOT wrapped: it stays an ``OperationalError`` so
    ``is_locked_error`` still routes it to "store busy — retry shortly", which
    is transient and must never be dressed up as damage.
    """


def damaged_store_recovery() -> str:
    """The one recovery for a corrupt store, worded once and shared.

    ``doctor`` prints this as its remediation and every command that hits a
    corrupt store prints it as its error, because the defect this replaces was
    precisely that those were two different sentences and only one of them
    worked: doctor said "Re-initialise: aisquare init", and ``aisquare init``
    crashed on the same corrupt file without repairing it.

    MOVED, not deleted, and not repaired. ``context.db`` holds the board's
    whole history; a diagnostic that destroys it is unrecoverable, so the
    operator performs the move and the broken file stays on disk for whoever
    wants to look at it. What the move costs is named here rather than left to
    be discovered afterwards.
    """
    db = paths.db_path()
    return (
        f"Move it aside and re-create: mv {db} {db}.broken && aisquare init "
        f"— the board history in it is lost; config.toml and credentials are untouched"
    )


def damaged_store_message(exc: sqlite3.Error) -> str:
    """The whole operator-facing sentence for a corrupt store: what and how.

    Built here rather than at each boundary because there are three of them —
    ``init``/``status`` via ``expected_store_errors`` and every team command
    via ``_fail_team`` — and three copies of a sentence is how doctor's
    remediation drifted from a working one in the first place.
    """
    return (
        f"the context store cannot be opened: {paths.db_path()} ({exc}). {damaged_store_recovery()}"
    )


def damaged_data_message(exc: sqlite3.Error) -> str:
    """For damage a QUERY found, where the file opened perfectly well.

    Deliberately not ``damaged_store_message``: that one says the store "cannot
    be opened", which is false here and would send the reader looking for the
    wrong thing — and §0b of the cutover runbook quotes it verbatim, so editing
    it in place would falsify an operator document nobody touched.

    The recovery is the same function, because the two sentences drifting apart
    is the defect this whole family exists to prevent.
    """
    return f"the context store is damaged: {paths.db_path()} ({exc}). {damaged_store_recovery()}"


def _statements(script: str) -> Iterator[str]:
    """Split a migration script into statements, using SQLite's own parser.

    ``sqlite3.complete_statement`` is the C tokenizer, so a semicolon inside a
    string literal or a ``CREATE TRIGGER … BEGIN … END`` body does not split
    the statement — which a regex would get wrong exactly once, on the day
    someone adds a trigger.
    """
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if buffer.strip() and sqlite3.complete_statement(buffer):
            yield buffer
            buffer = ""
    if buffer.strip():
        yield buffer


def _migrate(connection: sqlite3.Connection) -> None:
    """Bring the schema to the current version, safely under concurrency.

    Hooks race: several sessions can open (and try to migrate) the store in the
    same instant. Each migration runs as ONE ``BEGIN IMMEDIATE`` transaction
    that also bumps ``user_version`` — so a rebuild like v4 can never be
    observed half-done, and the write lock serialises racers.

    THE VERSION IS READ UNDER THE LOCK, and that ordering is the whole fix.
    Reading it first and then starting the transaction is a time-of-check /
    time-of-use gap: between the read and the ``BEGIN IMMEDIATE`` another opener
    can advance the schema, and this one then applies an OLD migration to a
    NEWER database. Measured before the fix, 12 concurrent first opens on a
    fresh store: 27 failures in 15 runs, ``duplicate column name: account`` —
    and instrumentation caught a thread running migration index 9 against a
    database that read version 8 on two independent connections. Nothing about
    it needed unusual load; it needed openers.

    ``executescript`` cannot be used for the transactional part, and that is the
    trap that makes the naive fix worse rather than better: it issues an
    implicit COMMIT *before* running its script, so a ``BEGIN IMMEDIATE`` taken
    beforehand is released instantly. The statements are therefore run one at a
    time under our own transaction. ``_statements`` splits them with SQLite's
    own parser, and a test pins that the schema this produces is byte-identical
    to what ``executescript`` produced.

    A loser whose script still fails re-reads the version: if another process
    advanced it, that's victory by other means; otherwise the error is real.
    """
    while True:
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) >= len(_MIGRATIONS):
            return
        try:
            connection.execute("BEGIN IMMEDIATE")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version >= len(_MIGRATIONS):
                connection.execute("COMMIT")
                return
            for statement in _statements(_MIGRATIONS[version]):
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {version + 1}")
            connection.execute("COMMIT")
        except sqlite3.Error:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            raise


def open_store() -> ContextStore:
    """Open (creating and migrating if needed) the context store.

    First opens race: parallel session hooks all arrive at a fresh database
    together, and the WAL switch + migrations contend for the write lock.
    ``busy_timeout`` covers most of it, but journal-mode changes can return
    "database is locked" without consulting the busy handler — so the whole
    setup phase retries with jitter. The retry budget scales with the
    ``AISQUARE_DB_BUSY_MS`` knob (3x it; 15s at the default) so a test that
    wedges a fresh store with knob=50 fails fast instead of sitting out a
    hardcoded floor. After the first successful open this loop never
    iterates.
    """
    paths.ensure_home()
    database = paths.db_path()
    # SQLite treats a ZERO-LENGTH file as a brand-new empty database, so a
    # truncated store is rebuilt, migrated, and reported healthy — measured:
    # `status` exits 0 in six lines and `doctor` says "✓ database: context.db is
    # readable", while every task, note and session it held is gone. It is the
    # only damage shape with no signal; the other four raise loudly.
    #
    # ABSENT IS NOT TRUNCATED. A new machine has no file at all and creating one
    # is correct. A file that EXISTS at zero length means something made it and
    # it lost what it held, which is the one moment that fact is knowable —
    # after this open the schema is back and the evidence is gone.
    #
    # Written straight to stderr rather than through the console helper: this
    # module is the data layer and imports nothing from the CLI, and the
    # doctrine for an observer that cannot fix what it sees is to say so on
    # stderr and carry on. Nothing is repaired, refused or deleted.
    if database.exists() and database.stat().st_size == 0:
        print(
            f"board: {database} exists but is empty — it was truncated, and the "
            "tasks, notes and sessions it held are gone. A new store is being "
            "created; this is not a fresh machine.",
            file=sys.stderr,
        )
        # That line goes to whoever opened the file — on a working machine a
        # HOOK, whose stderr neither the agent nor the operator reads. Record it
        # so `doctor` can answer the question the runbook teaches people to ask.
        # Fail-open: an observer may cost its own record, never the open.
        try:
            marker = paths.truncation_marker_path()
            marker.write_text(
                datetime.now(UTC).isoformat(timespec="seconds") + "\n", encoding="utf-8"
            )
        except OSError:
            pass
    connection = sqlite3.connect(str(database))
    connection.row_factory = sqlite3.Row
    busy_ms = _busy_timeout_ms()
    connection.execute(f"PRAGMA busy_timeout = {busy_ms}")
    deadline = time.monotonic() + 3 * (busy_ms / 1000)
    while True:
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA foreign_keys = ON")
            _migrate(connection)
            return SqliteStore(connection)
        except sqlite3.OperationalError as exc:
            if is_locked_error(exc):
                if time.monotonic() >= deadline:
                    connection.close()
                    raise  # transient: keeps its type, and its "retry shortly"
                time.sleep(0.05 + random.random() * 0.1)
                continue
            connection.close()
            raise StoreUnopenable(str(exc)) from exc
        except sqlite3.DatabaseError as exc:
            # Corruption (SQLITE_NOTADB) and a store wedged mid-migration both
            # land here. Measured before this existed: 59-75 lines of traceback
            # from ELEVEN commands, because they all die in this one place.
            connection.close()
            raise StoreUnopenable(str(exc)) from exc


@contextmanager
def store_session() -> Iterator[ContextStore]:
    """Open a store for the duration of a ``with`` block and close it after."""
    store = open_store()
    try:
        yield store
    finally:
        store.close()
