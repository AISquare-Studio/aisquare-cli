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
removal can propagate when sync lands. Every read filters tombstones out. A
project is forgotten the same way (``forgotten_at``, see v12); only
``purge_project`` deletes rows for real.
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import re
import shutil
import sqlite3
import sys
import time
import uuid
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import EllipsisType
from typing import Any, Literal, NamedTuple, Protocol

from aisquare.core import paths
from aisquare.core.ids import new_prompt_id
from aisquare.models import (
    CLAIM_KEEPING_STATUSES,
    CLOSED_STATUSES,
    UNKNOWN_KEY_UID,
    ClaudeAccountRecord,
    ContextEntry,
    EndedBy,
    FleetAgent,
    LaunchSpec,
    PendingRevocation,
    Pool,
    ProjectExplainability,
    ProjectGroup,
    ProjectInfo,
    PromptRecord,
    SessionUsage,
    TaskStatus,
    TeamEvent,
    TeamSession,
    TeamTask,
    TraceDestination,
    TurnMetric,
    TurnUsage,
    UsageSample,
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

# v11: the fleet — one row per agent the fleet started (a tmux pane + the role it
# runs + the board session id minted for it), and the project's fleet codename.
# The label is unique only among LIVE agents (partial index), so an ended agent
# frees its name. ``ALTER TABLE`` cannot add a UNIQUE constraint in SQLite, so the
# codename's uniqueness is an index too.
_SCHEMA_V11 = """
CREATE TABLE fleet_agent (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL,
    label         TEXT NOT NULL,
    role          TEXT NOT NULL,
    binary        TEXT NOT NULL DEFAULT 'claude',
    tmux_socket   TEXT NOT NULL DEFAULT 'asq',
    pane_id       TEXT NOT NULL,
    session_id    TEXT,
    cwd           TEXT NOT NULL,
    worktree      INTEGER NOT NULL DEFAULT 0,
    task_id       TEXT,
    spawned_by    TEXT,
    created_at    TEXT NOT NULL,
    ended_at      TEXT,
    exit_status   INTEGER
);
CREATE INDEX fleet_agent_project ON fleet_agent (project_id, created_at);
CREATE UNIQUE INDEX fleet_agent_live_label ON fleet_agent (project_id, label)
    WHERE ended_at IS NULL;
ALTER TABLE project ADD COLUMN codename TEXT;
CREATE UNIQUE INDEX project_codename ON project (codename);
"""

# v12: the Collective Intelligence test bed's per-turn metrics. One row per
# turn, opened by the UserPromptSubmit hook and closed by Stop, so a row exists
# whether or not CI was consulted — which is what makes the stretch before the
# endpoint goes live a usable baseline rather than a gap in the record.
#
# client_reason is here from creation, not bolted on later, because status and
# action cannot carry what it encodes: a timed-out call and a server answering
# `empty` both inject nothing. A table recording only the action makes an
# endpoint failing every single request look exactly like a clean baseline,
# and every aggregate over it measures plumbing rather than retrieval.
#
# The CHECKs pin the closed vocabularies — the server's (status, action,
# trigger, cache_status) are fixed by hook contract v2 and change only with a
# contract revision; client_reason and run_kind are ours, and a new value is a
# deliberate act that arrives with a migration. tests/test_store.py holds each
# list equal to the Python vocabulary it mirrors.
#
# There is no arm, no architecture, no flags hash and no run table: the client
# never sees an arm (the descriptor is blinding by construction), and a column
# for one here would be a place to leak it into. opaque_config_id is the only
# handle on which configuration served a turn, and it is recorded verbatim.
#
# Token and tool columns are nullable and stay unwritten until they come from
# real evidence (Explainability spans): hook payloads do not carry counts, and
# a fabricated number here is worse than a null because it survives into a
# published comparison.
#
# IF NOT EXISTS, and delivery_source inline rather than as a later ALTER,
# because this step is reached by databases that already have the table: this
# branch numbered the metric table v11 and v12 before `main` released v0.6.0
# with a *different* v11 (the fleet tables above). See _SCHEMA_V13.
_SCHEMA_V12 = """
CREATE TABLE IF NOT EXISTS metric (

    trace_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    session_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    wall_ms INTEGER,
    run_id TEXT,
    run_kind TEXT CHECK (run_kind IN ('live', 'replay')),
    opaque_config_id TEXT,
    trigger TEXT CHECK (trigger IN ('session_start', 'prompt_submit', 'agent_request')),
    client_reason TEXT NOT NULL DEFAULT 'disabled' CHECK (client_reason IN (
        'none', 'disabled', 'not_configured', 'no_run',
        'trigger_not_in_descriptor', 'no_prompt', 'no_session',
        'descriptor_unavailable', 'transport_error', 'deadline_exceeded', 'http_error',
        'malformed_body', 'contract_mismatch', 'schema_mismatch')),
    status TEXT CHECK (status IN ('served', 'empty', 'degraded', 'unavailable')),
    action TEXT CHECK (action IN ('inject', 'noop')),
    query_id TEXT,
    briefing_id TEXT,
    config_fingerprint TEXT,
    input_checkpoint TEXT,
    resolved_scope_version INTEGER,
    round_trip_ms INTEGER,
    server_ms INTEGER,
    deadline_breached INTEGER,
    token_count INTEGER,
    items_count INTEGER,
    cache_status TEXT CHECK (cache_status IN ('hit', 'miss', 'bypass')),
    error_codes TEXT NOT NULL DEFAULT '[]',
    rendered_chars INTEGER,
    injected_chars INTEGER,
    frame_version TEXT,
    instruction_version TEXT,
    redaction_level TEXT,
    snapshot_ref TEXT,
    snapshot_untracked_excluded INTEGER,
    tokens_in INTEGER,
    tokens_out INTEGER,
    tool_calls INTEGER,
    delivery_source TEXT CHECK (delivery_source IN ('descriptor', 'override'))
);

CREATE INDEX IF NOT EXISTS metric_project_started ON metric (project_id, started_at);
CREATE INDEX IF NOT EXISTS metric_open_session ON metric (session_id, started_at)
    WHERE ended_at IS NULL;
"""

# v13: converge the two v11s.
#
# `user_version 11` means one of two incompatible things in the wild, because
# two branches claimed it at once:
#   - v0.6.0 from PyPI: the fleet tables (_SCHEMA_V11 above) and no metric table;
#   - this branch, before the merge: the metric table and no fleet tables —
#     at 11 without delivery_source, or at 12 with it.
# Renumbering cannot serve both. Whichever meaning keeps the number, the other
# cohort's next migration hits a table that already exists (`table metric
# already exists`, `duplicate column name: delivery_source`) or silently never
# gets the tables it skipped, and a store that cannot open takes every command
# and every hook with it.
#
# So the ladder is made to converge instead of to count: V11 stays exactly as
# released (it only runs below 11, where neither table can exist), V12 creates
# the metric table only if it is absent, and this step gives the fleet tables to
# anyone who reached 11 or 12 down the CI branch and so never ran V11. Every
# cohort passes through here, so the end state is the same whichever way in.
# The two columns SQLite cannot add conditionally in SQL are in _PREPARE[12].
_SCHEMA_V13 = """
CREATE TABLE IF NOT EXISTS fleet_agent (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL,
    label         TEXT NOT NULL,
    role          TEXT NOT NULL,
    binary        TEXT NOT NULL DEFAULT 'claude',
    tmux_socket   TEXT NOT NULL DEFAULT 'asq',
    pane_id       TEXT NOT NULL,
    session_id    TEXT,
    cwd           TEXT NOT NULL,
    worktree      INTEGER NOT NULL DEFAULT 0,
    task_id       TEXT,
    spawned_by    TEXT,
    created_at    TEXT NOT NULL,
    ended_at      TEXT,
    exit_status   INTEGER
);
CREATE INDEX IF NOT EXISTS fleet_agent_project ON fleet_agent (project_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS fleet_agent_live_label ON fleet_agent (project_id, label)
    WHERE ended_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS project_codename ON project (codename);
"""

V1_ORPHAN_SUFFIX = "_v1_orphaned"
"""Where the v1-contract ``metric`` and ``run`` tables go at v12. Nothing reads
them; a developer who wants the space back drops them by hand."""


def _retire_v1_metric_table(connection: sqlite3.Connection) -> None:
    """Before the v12 statements: move a v1-contract ``metric`` table aside.

    Detected by shape, not by version — the version is the same 11 for every
    state above. A ``metric`` table without ``run_kind`` is the v1 one; it is
    renamed, its indexes dropped (they keep their names through a rename and
    would make the v12 ``CREATE INDEX IF NOT EXISTS`` a no-op on the new
    table), and the v1 ``run`` table renamed with it, so the schema that ends
    up stamped current is the v2 shape and nothing arm-shaped is live. Without
    this step every v12 statement would succeed on the v1 shape and stamp it 12
    — a database that can never take a row and can never be migrated by
    version again.
    """
    columns = {row[1] for row in connection.execute("PRAGMA table_info(metric)")}
    if not columns or "run_kind" in columns:
        return
    connection.execute("DROP INDEX IF EXISTS metric_project_started")
    connection.execute("DROP INDEX IF EXISTS metric_open_session")
    # A free name, never a fixed one: a rename onto an existing name raises,
    # the transaction rolls back, user_version stays 11 and every later open
    # fails the same way — a store nobody can use until a table is dropped by
    # hand. Two orphans are unlikely; a wedged store is certain if they occur.
    connection.execute(
        f"ALTER TABLE metric RENAME TO {_free_name(connection, f'metric{V1_ORPHAN_SUFFIX}')}"
    )
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "run" in tables:
        connection.execute(
            f"ALTER TABLE run RENAME TO {_free_name(connection, f'run{V1_ORPHAN_SUFFIX}')}"
        )


def _free_name(connection: sqlite3.Connection, base: str) -> str:
    """``base``, or ``base_2``, ``base_3`` … — the first name no table or index holds."""
    taken = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    name, suffix = base, 2
    while name in taken:
        name, suffix = f"{base}_{suffix}", suffix + 1
    return name


def _add_column_if_absent(
    connection: sqlite3.Connection, table: str, column: str, declaration: str
) -> bool:
    """``ALTER TABLE … ADD COLUMN`` only when the column is not already there.

    SQLite has no ``ADD COLUMN IF NOT EXISTS``, and a second ALTER raises
    ``duplicate column name``, which would abort the migration and leave the
    store unopenable. A missing *table* is not this function's business either:
    a cohort that has no ``metric`` yet gets the column from the CREATE in
    _SCHEMA_V12, so there is nothing to add.

    True when it added the column. A backfill that belongs to the column
    (:data:`_BACKFILLS`) runs then, and only then.
    """
    query = "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?"
    if connection.execute(query, (table,)).fetchone() is None:
        return False
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column in columns:
        return False
    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    return True


def _converge_v11_fork(connection: sqlite3.Connection) -> None:
    """Before v13's statements: add the columns the other v11 would have added.

    ``project.codename`` is missing for anyone who came down the CI branch (they
    never ran V11); ``metric.delivery_source`` is missing for anyone who stopped
    at this branch's old v11. Both are no-ops for a database that already has
    them, so every cohort can run this and land in the same shape.
    """
    _add_column_if_absent(connection, "project", "codename", "TEXT")
    _add_column_if_absent(
        connection,
        "metric",
        "delivery_source",
        "TEXT CHECK (delivery_source IN ('descriptor', 'override'))",
    )


def _index_board_stamps(connection: sqlite3.Connection) -> None:
    """v25 → v26's index on when each board event was written, for the reads of the events
    since a time that run every few seconds (``remote``'s needs scan). They started past
    the newest event stamped before that time on the ``seq`` index, and skipped an event
    written before a clock stepped back and stamped after the time asked (review of #259,
    round 6, 6/10). A store at v26 without it, an earlier 0.8.1 candidate's, gets it from
    the presence pass.

    Only where ``team_event`` is: a missing table is not this step's business, as it is
    not :func:`_add_column_if_absent`'s, and an index on it would leave the store
    unopenable rather than one doctor names the missing table of.
    """
    query = "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'team_event'"
    if connection.execute(query).fetchone() is not None:
        connection.execute(
            "CREATE INDEX IF NOT EXISTS team_event_project_created "
            "ON team_event (project_id, created_at)"
        )


def _index_task_numbers(connection: sqlite3.Connection) -> None:
    """v29 → v30: at most one task per number on a board (partial: NULL is no number), and
    the tasks with no number found without reading the table.

    :func:`_number_unnumbered_tasks` asks for one on every open, so on every hook, and
    ``number IS NULL`` cannot use the first index, whose rows are the numbered ones: a store
    with every task numbered read ``team_task`` whole to find none (review of #259, round
    13). The second holds only the tasks without a number, which is none once they are
    numbered. A store an earlier build of this line stamped v30 without it gets it from
    the presence pass at its next open (:data:`_PRODUCTS`).
    """
    query = "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'team_task'"
    if connection.execute(query).fetchone() is None:
        return
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS team_task_number ON team_task (project_id, number) "
        "WHERE number IS NOT NULL"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS team_task_unnumbered ON team_task (project_id) "
        "WHERE number IS NULL"
    )


# Python that must run before a migration's statements, inside its transaction,
# keyed by the version being upgraded FROM. Kept apart from _MIGRATIONS so the
# scripts stay plain SQL that executescript and _statements build identically.
_PREPARE: dict[int, Callable[[sqlite3.Connection], None]] = {
    11: _retire_v1_metric_table,
    12: _converge_v11_fork,
    25: _index_board_stamps,
    29: _index_task_numbers,
}


def _adopt_onboarded_projects(connection: sqlite3.Connection) -> None:
    """v16 → v17's backfill (#139): rows already used on purpose become onboarded.

    Runs right after ``project.onboarded_at`` is added, in the same transaction,
    and only then (:data:`_BACKFILLS`): by v17, or by the presence pass for a
    store another line stamped past 17 without ever running it.
    "On purpose" is read off what the row already carries: context entries, a
    codename (it entered the fleet), linked repos, board activity, a fleet
    agent — and a codebase snapshot on disk, which ``project onboard`` and ``init``
    wrote up to 0.8.2 (:func:`_snapshot_on_disk`). Everything else — the
    23 of 27 registrations on the reporting machine that held nothing but
    captured prompts — stays hidden, reachable through ``project list --all``.

    A FORGOTTEN row is never adopted, whatever it carries: ``forget`` clears
    ``onboarded_at`` so that the next prompt there revives the row captured,
    not listed (:meth:`SqliteStore.ensure_project`), and a row forgotten before
    the column existed must land in that same state — a forgotten project
    keeps its entries and its snapshot, so the evidence below would otherwise
    re-list it on the next prompt.
    """
    connection.execute(
        "UPDATE project SET onboarded_at = created_at "
        "WHERE onboarded_at IS NULL AND forgotten_at IS NULL AND ("
        "  codename IS NOT NULL"
        "  OR (linked_repos IS NOT NULL AND linked_repos NOT IN ('[]', ''))"
        "  OR id IN (SELECT project_id FROM entry WHERE project_id IS NOT NULL)"
        "  OR id IN (SELECT project_id FROM team_event)"
        "  OR id IN (SELECT project_id FROM team_task)"
        "  OR id IN (SELECT project_id FROM fleet_agent)"
        ")"
    )
    rows = connection.execute(
        "SELECT id, root FROM project WHERE onboarded_at IS NULL AND forgotten_at IS NULL"
    ).fetchall()
    # Never the aisquare home's own row: builds before D5 packed the home too, and that
    # pack is no sign the owner added it (main's snapshot.exists() said the same). The
    # stat first: only a row with a pack has its root resolved, so roots on dead mounts
    # cost nothing.
    with_snapshot = [
        row[0] for row in rows if _snapshot_on_disk(str(row[0])) and not is_the_home(Path(row[1]))
    ]
    for project_id in with_snapshot:
        connection.execute(
            "UPDATE project SET onboarded_at = created_at WHERE id = ? AND onboarded_at IS NULL",
            (project_id,),
        )


def _snapshot_on_disk(project_id: str) -> bool:
    """Whether the project's old pack is on disk; one that cannot be looked at is no evidence.

    Builds up to 0.8.2 packed a project on ``project onboard`` and on ``init``, and
    wrote ``projects/<id>/snapshot/snapshot.json`` beside the pack. The packer is gone,
    but a store from 0.6 or older still reaches this backfill, and for a project the
    owner added and never used, that file is its only mark. So the check is a plain
    path, with no packer to import, and :func:`_remove_old_packs` deletes the packs
    only after :func:`_migrate` has run this.

    ``Path.exists`` answers ``False`` for a path that is not there and RAISES for a
    snapshot directory this user cannot search (``PermissionError``). Raised out of
    the backfill, it escaped the migration's ``sqlite3.Error`` handler with the
    transaction still open, so every command failed on the open with a raw
    traceback until the directory was readable again (review of #203). The row
    stays captured, as one without a snapshot does, and a later ``project
    onboard`` adds it.
    """
    try:
        return (paths.project_data_dir(project_id) / "snapshot" / "snapshot.json").exists()
    except OSError:
        return False


#: Written in ``cache/`` once :func:`_remove_old_packs` has run, so each later open
#: costs one stat. Disposable: run again, it finds nothing to delete.
_OLD_PACKS_REMOVED = "old-packs-removed"


def _remove_old_packs() -> None:
    """Delete the codebase packs builds up to 0.8.2 left in ``projects/<id>/snapshot/``, once.

    Called by :func:`open_store` after :func:`_migrate`, and never before it: the v17
    backfill reads a pack's ``snapshot.json`` as evidence (:func:`_snapshot_on_disk`),
    and a pack deleted before an old store migrates takes that evidence with it.

    Only a directory named ``snapshot``, directly in a project's data directory, and
    only while it resolves to one inside ``projects/``: a symlink is never followed out
    of the home, and ``brain/`` beside it is never touched. Best effort: what cannot be
    deleted stays, and the open goes on. ``RuntimeError`` too: on Python 3.11 and 3.12
    ``Path.resolve()`` raises it for a symlink loop, and a ``projects/`` that points at
    itself made every command a traceback (review of #399).

    Done is recorded only once ``projects/`` was listed, or is not there at all. On
    3.13 that loop resolves without raising and ``is_dir()`` swallows the ELOOP, so
    asking ``is_dir()`` first marked a ``projects/`` it never read as done for good.
    """
    marker = paths.cache_dir() / _OLD_PACKS_REMOVED
    try:
        if marker.exists():
            return
        inside = (paths.aisquare_home() / "projects").resolve()
        try:
            projects = list(inside.iterdir())
        except FileNotFoundError:
            projects = []
        for project in projects:
            pack = project / "snapshot"
            with contextlib.suppress(OSError, RuntimeError):
                if (
                    pack.is_dir()
                    and not pack.is_symlink()
                    and pack.resolve().parent.parent == inside
                ):
                    shutil.rmtree(pack, ignore_errors=True)
        marker.touch()
    except (OSError, RuntimeError):
        pass


def _bind_keys_to_their_destinations(connection: sqlite3.Connection) -> None:
    """v23 → v24's backfill (#142): a key bound to its destination's deployment says so.

    Runs right after ``project_explainability.api_url`` is added, in the same
    transaction, and only then (:data:`_BACKFILLS`). A binding whose target is
    the deployment the project's destination names now was minted for it, or
    attached by ``key set``, which binds to the destination's deployment by
    default, so it gets that destination's API. Every other binding stays NULL:
    bound to one of the machine's targets. A key the operator had bound to the
    machine's target before choosing a destination of the same name is marked
    too. It then answers for that destination as it did, and after ``use
    --clear`` it no longer answers for the machine's target, whose deployment it
    may not be: the machine key applies there instead.
    """
    connection.execute(
        "UPDATE project_explainability SET api_url = ("
        "  SELECT destination.api_url FROM project_destination AS destination"
        "  WHERE destination.project_id = project_explainability.project_id"
        "  AND destination.environment = project_explainability.target"
        ") WHERE api_url IS NULL"
    )


def _number_existing_tasks(connection: sqlite3.Connection) -> None:
    """v28 → v29's backfill: every task already on a board gets its number, 1, 2, … per
    project in the order the tasks were added (``created_at``, the id breaking a tie).

    Runs right after ``team_task.number`` is added, in the same transaction, and only
    then (:data:`_BACKFILLS`), so a store that has its numbers is never renumbered: a
    number once said on a board keeps meaning that task.
    """
    connection.execute(
        "UPDATE team_task SET number = ("
        "  SELECT ranked.n FROM ("
        "    SELECT id, ROW_NUMBER() OVER ("
        "      PARTITION BY project_id ORDER BY created_at, id"
        "    ) AS n FROM team_task"
        "  ) AS ranked WHERE ranked.id = team_task.id"
        ") WHERE number IS NULL"
    )


def _number_unnumbered_tasks(connection: sqlite3.Connection) -> None:
    """Every task stored without a number gets one on open: its board's highest plus one,
    in the order the tasks were added (``created_at``, the id breaking a tie).

    v29's backfill numbers what a store holds when the column is added, and only then. A
    build from before v29 opens a v30 store without complaint and inserts with ``number``
    NULL, so the tasks it adds would show an id tail and an empty ``#`` for good (review
    of #261, m6). Each one is numbered as an add here numbers it (:meth:`SqliteStore.
    upsert_task`), under the write lock, so a racing add or opener cannot take the same
    number. A store with every task numbered pays two reads, the second of an empty index
    (``team_task_unnumbered``, :func:`_index_task_numbers`), and a store without
    ``team_task`` (doctor's damaged-store case) is left to be described.
    """
    query = "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'team_task'"
    if connection.execute(query).fetchone() is None:
        return
    query = "SELECT 1 FROM team_task WHERE number IS NULL LIMIT 1"
    if connection.execute(query).fetchone() is None:
        return
    try:
        connection.execute("BEGIN IMMEDIATE")
        unnumbered = connection.execute(
            "SELECT id FROM team_task WHERE number IS NULL ORDER BY created_at, id"
        ).fetchall()
        for row in unnumbered:
            connection.execute(
                "UPDATE team_task SET number = (SELECT COALESCE(MAX(number), 0) + 1 "
                "FROM team_task AS board WHERE board.project_id = team_task.project_id) "
                "WHERE id = ?",
                (row[0],),
            )
        connection.execute("COMMIT")
    except sqlite3.Error:
        with contextlib.suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        raise


def _has_table(connection: sqlite3.Connection, table: str) -> bool:
    query = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?"
    return connection.execute(query, (table,)).fetchone() is not None


#: The board of a ``fleet_agent`` row that recorded none: that of the session bound to it,
#: the one its hooks registered on, else its own project's. The one answer for the v32
#: backfill (:func:`_board_existing_agents`) and for every read of a row written with no
#: board since (:data:`_FLEET_AGENT_READ`): a 0.8.0 build opens a v32 store as it is, and
#: its spawn writes NULL there. Read as the row's project, such a row under a hub announced
#: its exit, and took its pause, on a board its manager does not read, where the backfill
#: gave a row from before v32 the hub's (review of #259, round 26, 9/11).
_UNRECORDED_BOARD = (
    "COALESCE((SELECT session.project_id FROM team_session AS session"
    " WHERE session.id = fleet_agent.session_id), fleet_agent.project_id)"
)


def _board_existing_agents(connection: sqlite3.Connection) -> None:
    """v31 → v32's backfill: every fleet agent already recorded reads the board of the
    session bound to it, the one its hooks registered on, else its own project's
    (:data:`_UNRECORDED_BOARD`).

    Runs right after ``fleet_agent.board`` is added, in the same transaction, and only then
    (:data:`_BACKFILLS`). A store without ``team_session`` (doctor's damaged-store case)
    gives every row its project's.
    """
    if not _has_table(connection, "team_session"):
        connection.execute("UPDATE fleet_agent SET board = project_id WHERE board IS NULL")
        return
    connection.execute(f"UPDATE fleet_agent SET board = {_UNRECORDED_BOARD} WHERE board IS NULL")


#: Backfills that belong to a column, keyed ``(table, column)``. Each runs in the
#: transaction that adds its column, right after the ALTER, and only when the
#: column was actually added: by its step on the ladder or by
#: :func:`_converge_by_presence`, whichever gets there. Rows that predate the
#: column get their value from what they already carry, and a store that
#: already has the column is not backfilled again. That makes a step met twice
#: a no-op, and the presence pass, which runs on every open, changes no row.
#: A step's statements have all run by then (:func:`_run_step`).
_BACKFILLS: dict[tuple[str, str], Callable[[sqlite3.Connection], None]] = {
    ("project", "onboarded_at"): _adopt_onboarded_projects,
    ("project_explainability", "api_url"): _bind_keys_to_their_destinations,
    ("team_task", "number"): _number_existing_tasks,
    ("fleet_agent", "board"): _board_existing_agents,
}

# v14: ``project forget`` — a tombstone on the registration, in the same spirit as
# ``entry.deleted_at``. A hard delete is not available to a plain forget:
# ``entry.project_id`` and ``prompt.project_id`` are FOREIGN KEYS to this row
# with no cascade and ``PRAGMA foreign_keys = ON``, so a project with any
# context or prompt history cannot be deleted from under them. Every project
# read filters ``forgotten_at IS NOT NULL`` out; ``ensure_project`` clears it, so
# a registration comes back the moment something registers the root again —
# since v17 as a captured, unlisted row unless the registration is deliberate.
# ``forget --purge`` deletes the dependents first and then the row, for real.
#
# v14, not v12 as first authored: main took v12 for the metric table and v13
# to converge the two v11s while this branch was open — the third time a
# number was claimed twice. A plain ALTER is safe to run last whatever route
# a store took to 13; every earlier step leaves the project table in place.
_SCHEMA_V14 = """
ALTER TABLE project ADD COLUMN forgotten_at TEXT;
"""

# v15 onward: every step is IDEMPOTENT, and what it produces is also looked for
# by name after the ladder (:data:`_PRODUCTS`, :func:`_converge_by_presence`).
# Other lines of development claimed v15-v17 while this one was in flight, as
# v11 was claimed twice before them: #136 stamps 15 for ``work_brief``, #201
# stamps 15 for the persona columns, and #113 stamps 15-17 for its coding-agent
# columns. ``_migrate`` counts positionally, so a store stamped 15 or 17 by one
# of those lines never runs this line's steps below its stamp. It has no
# ``claude_account``, and every accounts command fails with "no such table";
# stamped 17, it has no ``onboarded_at`` either, and v23 fails on that column,
# so the store stops opening. Renumbering would strand the stores that ran
# these steps under these numbers (the accounts stack's own), so the numbers
# stay and the ladder converges instead: each CREATE is ``IF NOT EXISTS``, each
# column is added by :func:`_add_column_if_absent` (a script cannot add a column
# conditionally, so the columns are listed in :data:`_PRODUCTS`, not written as
# ALTERs here), and a step whose products a store lacks is applied again,
# whatever ``user_version`` says.
#
# v15: the Claude account REGISTRY and per-project settings (#145).
#
# ``claude_account`` is the operator's arrangement of the account slots — an
# alias, a priority order, the machine default, a disabled flag — and nothing
# else. The slots themselves stay directories under ``~/.aisquare/claude-accounts``
# (docs/plans/claude-accounts.md §2: "the directories are the record"); this table
# holds only what a directory cannot: a name and a rank. ``slot`` is the join,
# ``config_dir`` is recorded for the launch record and never consulted, and a row
# whose directory is gone is pruned on the next sync rather than trusted.
#
# Two invariants are the DATABASE's, not the service's, because both were once
# left to callers in this repo and both drifted: at most ONE default (a partial
# unique index on ``is_default = 1``) and no two slots with the same alias
# (a partial unique index on ``alias``). ``ALTER TABLE`` cannot add a UNIQUE
# constraint in SQLite, and a CHECK cannot span rows, so both are indexes.
#
# ``project_setting`` is a small key/value table per project, introduced here for
# one key, ``claude_account`` (the project's default account). #141 and #142 were
# planned as keys here and each has a table of its own instead: v19's
# ``project_explainability`` and v21's ``project_destination``. A key binding and
# a destination need typed columns and exactly one row per project, which a
# key/value row cannot hold for them. This table is for plain per-project values.
#
# ``fleet_agent.account_slot`` (in :data:`_PRODUCTS`) records which account a
# fleet window was launched under, as resolved at spawn: the flag, the role
# binding, the project default or the machine default. ``team_session.account``
# (v8) carries the config DIRECTORY once the session's first hook reports a
# transcript path; this is the slot, known before the agent has said a word,
# which is what a restart (#144) or a hand-over (#146) needs.
_SCHEMA_V15 = """
CREATE TABLE IF NOT EXISTS claude_account (
    slot        INTEGER PRIMARY KEY,
    config_dir  TEXT NOT NULL,
    alias       TEXT,
    position    INTEGER NOT NULL,
    is_default  INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    disabled    INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)),
    created_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS claude_account_alias ON claude_account (alias)
    WHERE alias IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS claude_account_default ON claude_account (is_default)
    WHERE is_default = 1;

CREATE TABLE IF NOT EXISTS project_setting (
    project_id  TEXT NOT NULL REFERENCES project (id),
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    set_at      TEXT NOT NULL,
    PRIMARY KEY (project_id, key)
);
"""

# v16: usage-aware accounts (#146).
#
# ``claude_usage`` keeps the readings of each account's two rate-limit windows
# so a RATE can be computed — "at this pace the five-hour window is full in 40
# minutes" — which one reading cannot say. Written whenever usage is fetched
# (the Accounts page's minute tick, `accounts usage`, a headroom pick); rows
# older than a week are pruned on write. Derived convenience, never the record:
# a missing table costs a trend line, nothing else.
#
# ``team_session.limit_resets_at`` (in :data:`_PRODUCTS`) carries the reset time
# a usage-limit error named, for the ``limited`` state the StopFailure hook
# writes; it is read only while ``state = 'limited'`` and a prompt that lifts
# the session back to ``working`` leaves the stale time behind unread.
_SCHEMA_V16 = """
CREATE TABLE IF NOT EXISTS claude_usage (
    slot               INTEGER NOT NULL,
    fetched_at         TEXT NOT NULL,
    session_percent    REAL,
    session_resets_at  TEXT,
    week_percent       REAL,
    week_resets_at     TEXT
);
CREATE INDEX IF NOT EXISTS claude_usage_slot_time ON claude_usage (slot, fetched_at);
"""

# v17: captured is not shown (#139). Hooks register every directory a session
# runs in — that must stay, prompt history and injection depend on it — but only
# a project added ON PURPOSE (init, project onboard/link, the sidebar's +, team
# on, a fleet spawn) carries ``onboarded_at``, and only those are listed. The
# column is the whole step, so the script is empty: the column is in
# :data:`_PRODUCTS`, its backfill (:func:`_adopt_onboarded_projects`) in
# :data:`_BACKFILLS`.
_SCHEMA_V17 = ""
# v18 (#144): the launch spec a restart replays, and a home for UI state that
# used to live in scattered files — one key/value table, read at mount and
# written on change, so what was open comes back after a restart. The column,
# ``fleet_agent.launch_spec``, is in :data:`_PRODUCTS`.
_SCHEMA_V18 = """
CREATE TABLE IF NOT EXISTS ui_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
# v19 (#141): a project's own explainability key — its deployment and where the
# mode-600 file is. Never the value: this database is mode 644.
_SCHEMA_V19 = """
CREATE TABLE IF NOT EXISTS project_explainability (
    project_id TEXT PRIMARY KEY REFERENCES project (id),
    target     TEXT NOT NULL,
    key_path   TEXT NOT NULL,
    set_at     TEXT NOT NULL,
    set_by     TEXT
);
"""
# v20 (#140): project groups, pinning and manual order — a management layer over
# projects. One group per project (like a browser tab), positions per scope,
# pins on projects and groups. Nothing else references a group. The project's
# three columns, ``group_id``, ``position`` and ``pinned_at``, are in
# :data:`_PRODUCTS`, added once this table exists.
_SCHEMA_V20 = """
CREATE TABLE IF NOT EXISTS project_group (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    position   INTEGER NOT NULL DEFAULT 0,
    pinned_at  TEXT,
    collapsed  INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""
# v21 (#142): where a project's traces land — a workspace and a studio picked by a
# signed-in user, from the API environment the session belongs to. Its own table
# rather than new columns on ``project_explainability``: that row is the KEY
# binding (#141) with a NOT NULL key path, and a destination exists before, or
# without, any key — widening it would mean a rebuild migration and a nullable
# path every reader of the binding would then have to reason about. Two rows
# per project at most, each with one job; the resolver joins them by target.
_SCHEMA_V21 = """
CREATE TABLE IF NOT EXISTS project_destination (
    project_id     TEXT PRIMARY KEY REFERENCES project (id),
    api_url        TEXT NOT NULL,
    environment    TEXT NOT NULL,
    workspace_id   INTEGER NOT NULL,
    workspace_uid  TEXT,
    workspace_name TEXT NOT NULL,
    studio_id      INTEGER,
    studio_uid     TEXT,
    studio_name    TEXT,
    key_uid        TEXT,
    set_at         TEXT NOT NULL,
    set_by         TEXT
);
"""
# v22 (#142, review of the accounts stack's fold): the revocations still owed for
# keys the CLI minted. A minted key's uid lived on its destination row alone, and
# every path that took it off — a move, `use --clear`, `key set`, `key clear`, a
# new mint, a purge, `logout` — dropped it whether or not the server had revoked
# the key, so a revoke that could not be made (signed out, another host, offline)
# left a live `ingest:write` key nothing here remembered. A row is written in the
# transaction that detaches the key and deleted on the server's confirmation.
# No foreign key to `project`: the record must outlive a purge of its project,
# which is one of the ways a key gets here. IF NOT EXISTS, so a store that meets
# this step twice (a cohort renumbered onto it) converges instead of failing.
_SCHEMA_V22 = """
CREATE TABLE IF NOT EXISTS pending_revocation (
    key_uid        TEXT PRIMARY KEY,
    api_url        TEXT NOT NULL,
    workspace_id   INTEGER NOT NULL,
    workspace_name TEXT NOT NULL,
    project_id     TEXT NOT NULL,
    project_name   TEXT NOT NULL,
    detached_at    TEXT NOT NULL,
    last_error     TEXT
);
"""
# v23 (#139 and #140, review of #168 at the fold): the one-time repair of the
# tombstones two early cuts left holding what a revival must not bring back. The
# first cut of the v17 backfill (c716094) had no ``forgotten_at`` guard and
# stamped forgotten rows with history as onboarded, and a forget written before
# #171's first round kept the group, the position and the pin. Each was answered
# per write instead: a CASE in the capture every prompt runs, and in the
# onboarding, for a state only stores that met those cuts hold. Cleared here
# once, as ``forget_project`` clears them now, a tombstone carries none of the
# four, and nothing writes them onto one (``update_project_layout`` refuses a
# forgotten row), so a revival keeps a live row's values and has no tombstone
# left to reason about. A plain UPDATE: a store that meets it twice converges.
_SCHEMA_V23 = """
UPDATE project SET onboarded_at = NULL, group_id = NULL, position = NULL, pinned_at = NULL
WHERE forgotten_at IS NOT NULL;
"""
# v24 (#142, review of #203): which deployment a project's key was attached FOR.
# ``project_explainability.api_url`` is the API of the project's destination when
# the key was minted or attached for that destination's deployment, and NULL for
# a key bound to one of the machine's targets. The binding named its deployment
# by target name alone, and one name can mean two deployments: the destination's
# ``stg`` is staging, while on the machine ``init --explainability`` writes the
# machine's ``stg`` is the top-level prod gateway. A staging key attached while
# the project pointed at staging answered, after ``use --clear``, as the
# machine's ``stg`` and went to prod. The column is the whole step, so the script
# is empty: the column is in :data:`_PRODUCTS`, its backfill
# (:func:`_bind_keys_to_their_destinations`) in :data:`_BACKFILLS`.
_SCHEMA_V24 = ""
# v25 (round 25 of #257): where a fleet agent's server listened and in which boot,
# recorded at spawn. Uninstall and upgrade ask whether a live row may still run, and
# inferred it from the asking shell: tmux's default path under ITS ``TMUX_TMPDIR``,
# and the wall-clock boot time, which moves when the clock is stepped. Both inferences
# failed toward removing the hooks under running agents. The columns are the whole
# step, so the script is empty: they are in :data:`_PRODUCTS`; a row from before them
# keeps NULLs, which ``services.lifecycle`` reads as nothing recorded.
_SCHEMA_V25 = ""
# v26 (follow-up of #257): when a fleet agent's server started, as it said at spawn
# (``#{start_time}``): the identity of the server its pane id names. A server that
# answers at the row's socket but started at another time took the path from a server
# that lost its socket file and may run on, so stop, shutdown, reap and uninstall hold
# the row instead of ending it under its agent. The column is in :data:`_PRODUCTS`; a row
# from before it keeps NULL, which every reader takes as nothing recorded. And an index on
# when each board event was written (:func:`_index_board_stamps`, in :data:`_PREPARE`), so
# the script is empty.
_SCHEMA_V26 = ""
# v27: a project's nickname — free text a person gives it, as they name a browser tab,
# beside the codename that names its tmux session and branches and so stays constrained.
# The column is the whole step, so the script is empty: it is in :data:`_PRODUCTS`; a
# row from before it keeps NULL, which every reader shows as the directory's name. The
# ``name`` column is not it: written once as the basename and never read, it is not
# repurposed.
_SCHEMA_V27 = ""
# v28: which token a usage reading was taken with — a fingerprint of it, never the token.
# The five-minute cache (``services.claude_accounts.cached_usage``) serves a recorded
# reading only to the token that took it: a slot signed into another login, or signed
# out, is asked again rather than shown the last login's figure. The column is the whole
# step, so the script is empty: it is in :data:`_PRODUCTS`; a row from before it keeps
# NULL, which the cache serves to no token, and which the trends read as before.
_SCHEMA_V28 = ""
# v29: a task's number on its board (``#12``) — what a person reads and types instead of
# ``tsk_01m4wd5p…``. Per project, 1, 2, … in the order tasks are added: given at insert
# as the project's highest plus one, in the INSERT itself (one statement under the write
# lock), and never reused, since tasks leave a board only with the whole project. The
# column is the whole step, so the script is empty: it is in :data:`_PRODUCTS`, and its
# backfill (:func:`_number_existing_tasks`) numbers the tasks a store already holds.
_SCHEMA_V29 = ""
# v30: one number per board — a unique index (:func:`_index_task_numbers`). A step of its
# own because a step's script runs BEFORE its columns are added (:func:`_run_step`): in
# v29 it would name a column that is not there yet. Python, not a script, because the
# presence pass applies a step to a store whatever it lacks, and a store without
# ``team_task`` at all (doctor's damaged-store case) must still open and be described,
# as :func:`_add_column_if_absent` already lets it for a column.
_SCHEMA_V30 = ""
# v31 (review of #259, round 15): the branch a fleet agent's worktree was on, as git said
# at spawn, at stop and at reap before the tree went: where a restart or switch makes a
# gone tree again. It was looked for in git's worktree list, which every spawn's prune
# empties, and in notes in the repo's git config keyed by a path labels reuse, which went
# stale. The column is the whole step, so the script is empty: it is in :data:`_PRODUCTS`;
# a row from before it keeps NULL, which a restart of a gone tree reads as no branch known.
_SCHEMA_V31 = ""
# v32 (review of #259, round 25): the board a fleet agent's sessions read, as its spawn
# gave it: the hub's under ``AISQUARE_TEAM_HUB``, else its project's principal checkout's.
# Worked out again from the process environment, the bound session, the cwd or Remote's
# own environment, each reader had its own answer. The column is the whole step, so the
# script is empty: it is in :data:`_PRODUCTS`, and its backfill
# (:func:`_board_existing_agents`) gives a row from before it its session's board.
_SCHEMA_V32 = ""
# v33 (#201, docs/plans/spawn-personas.md §5): the persona a session was launched as and
# the one a fleet agent was spawned with. Nullable: no persona is the default and the
# common case, and a row records what was ASKED even when the persona could not be
# loaded. The persona line stamped these columns as its own v15 (#208 converged that
# number); stamped here, a store of that line holds them already, and so does the
# captain RC's v24, so the columns are the whole step and the script is empty: they are
# in :data:`_PRODUCTS`, added only where absent, and a row from before them keeps NULL,
# which reads as no persona.
_SCHEMA_V33 = ""
# v34 (#38): how a fleet agent's row ended — ``stopped`` when aisquare ended it on purpose
# (``fleet stop``, Stop all, ``shutdown``, a hand-over's old row), ``died`` when a read or
# ``reap`` recorded a death nobody asked for (killed, crashed, its server gone). Restart all
# brings the second back and leaves the first. The column is the whole step, so the script
# is empty: it is in :data:`_PRODUCTS`, and a row from before it keeps NULL, which
# ``services.fleet`` reads by its exit status.
_SCHEMA_V34 = ""
# v35 (token metrics, #346): what every session actually spent, read off its transcript.
#
# ``usage_session`` is one row per agent session the hooks saw, carrying the scan's
# STATE (byte offsets, the message ids already counted, the first-edit flag) beside its
# running totals: a Stop reads only what was appended since the last one. ``usage_turn``
# is one row per Stop-delimited stretch, and names the ``metric`` row it closed when
# there was one (``trace_id``). The breakdowns (per model, per tool) are JSON because
# they are read whole and never queried into; the totals beside them are columns, for
# a reader with sqlite3 and nothing else.
#
# #346 stamped this step v34 on its own branch, beside #38's v34 here (#42 merged the
# two). A store that ran #346's branch reads v34 with these tables and no
# ``fleet_agent.ended_by``: the presence pass (:func:`_converge_by_presence`) adds the
# column before this step, which then finds its tables and indexes in place. A store
# that ran #38's v34 gets them here. Every statement is ``IF NOT EXISTS`` either way.
_SCHEMA_V35 = """
CREATE TABLE IF NOT EXISTS usage_session (
    session_id      TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    transcript_path TEXT,
    state           TEXT NOT NULL DEFAULT '{}',
    usage           TEXT NOT NULL DEFAULT '{}',
    turns           INTEGER NOT NULL DEFAULT 0,
    total_tokens    INTEGER NOT NULL DEFAULT 0,
    weighted        REAL NOT NULL DEFAULT 0,
    cost_usd        REAL NOT NULL DEFAULT 0,
    started_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    ended_at        TEXT
);
CREATE INDEX IF NOT EXISTS usage_session_project ON usage_session (project_id, updated_at);

CREATE TABLE IF NOT EXISTS usage_turn (
    id           TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    project_id   TEXT NOT NULL,
    trace_id     TEXT,
    seq          INTEGER NOT NULL,
    trigger      TEXT NOT NULL CHECK (trigger IN ('stop', 'stop_failure', 'session_end')),
    recorded_at  TEXT NOT NULL,
    usage        TEXT NOT NULL,
    total_tokens INTEGER NOT NULL,
    weighted     REAL NOT NULL,
    cost_usd     REAL NOT NULL,
    scan_ms      REAL
);
CREATE INDEX IF NOT EXISTS usage_turn_session ON usage_turn (session_id, seq);
"""
# v36 (P0-W1a, #122): whether a fleet agent's first message started a turn. The session's
# last ``UserPromptSubmit`` (``team_session.prompted_at``), and on the agent's row where its
# first message stands (``fleet_agent.first_prompt``: pending, acked, unsent) and since when
# (``first_prompt_at``). Nullable: a row from before them reads as "not tracked", never as
# a message that was not taken. The columns are the whole step, so the script is empty:
# they are in :data:`_PRODUCTS`, added only where absent.
#
# rc/p0-reliability stamped this step v34, beside main's #38 v34 and #346 v35 (the sync
# of main 8eb18a8a moved it here). A store that ran rc/p0 reads v34 with these columns and
# neither ``fleet_agent.ended_by`` nor the usage tables: the presence pass
# (:func:`_converge_by_presence`) adds the column, the ladder runs v35's ``IF NOT EXISTS``
# script, and this step finds its columns in place.
_SCHEMA_V36 = ""

# v37, P1 WS-2 (#279): who caused a board event (``team_event.actor``) and its details as
# JSON (``team_event.meta``), so a manager's own stop wakes nobody and an exit says its
# status. Both columns are nullable and in :data:`_PRODUCTS`, added only where absent, so a
# store another line stamped at any version still gains them; a row from before them reads
# NULL: no actor (the agent's own act, or unknown) and no details.
#
# rc/p1-token-diet stamped this step v35, after its own v34 (P0-W1a, now v36): the sync of
# main 8eb18a8a moved it here, after main's v34 and v35. A store that ran rc/p1 reads v35
# with P0-W1a's and these columns and neither ``fleet_agent.ended_by`` nor the usage
# tables: the presence pass adds the column, v35's ``IF NOT EXISTS`` script makes the
# tables, and v36 and this step find their columns in place.
_SCHEMA_V37 = ""
# v38 (P2-W6, #199): a parent manager over a project group. ``project_group`` gains its
# parent project, its routing rules (JSON) and its agent cap; ``team_task`` gains the task
# it was delegated from (its origin and that origin's project). All nullable and in
# :data:`_PRODUCTS`, added only where absent, so a store another line carried past v37
# converges too; a row from before them reads NULL: no parent, no rules, no cap, no origin.
# #140's "nothing else references a group" is amended by exactly these linkage columns.
#
# rc/p2-capacity-setup stamped this step v36, after its own v34 (P0-W1a, now v36) and v35
# (P1 WS-2, now v37): the sync of main 8eb18a8a moved it here, after main's v34 and v35. A
# store that ran rc/p2 reads v36 with P0-W1a's, P1 WS-2's and these columns and neither
# ``fleet_agent.ended_by`` nor the usage tables: the presence pass adds the column and
# runs v35's ``IF NOT EXISTS`` script for the tables, and v37 and this step find their
# columns in place.
_SCHEMA_V38 = ""

# v39 (docs/plans/evidence-gate.md, part 2): work briefs. One row per brief, its
# requirements, links, evidence and corrections as one JSON document, so every write
# compares ``revision`` and replaces the document whole (:meth:`SqliteStore.save_brief`).
# Named ``brief``, not ``work_brief``: #136's builds stamped their own v15 for a
# ``work_brief (id, project_id, revision, data)`` table, and ``IF NOT EXISTS`` would
# keep that table on such a store, without this one's ``number`` or timestamps. Its
# rows are left where they are, unread. The script creates the table and its index,
# both in :data:`_PRODUCTS`.
#
# #306 and #341 stamped this step v34 on their own branches, beside #38's v34 and #346's
# v35 here, and v36 once they merged main's v35. #417 stamped it v37, after P0-W1a's v36,
# and P1 WS-2's v37 above moved it to v38, and main's P2-W6 took v38 (above) in the same
# release, so the final merge moved it here, to v39. A store that ran any of them reads
# v34, v36, v37 or v38 with ``brief``, and lacks what a step under its stamp adds:
# ``fleet_agent.ended_by`` (v34), P0-W1a's columns (v36), P1 WS-2's (v37) or P2-W6's
# (v38). The presence pass (:func:`_converge_by_presence`) adds those, the ladder runs
# the steps above the stamp, and this step then finds ``brief`` and its index in place:
# every statement is ``IF NOT EXISTS``, so it is idempotent.
_SCHEMA_V39 = """
CREATE TABLE IF NOT EXISTS brief (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    number INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS brief_number ON brief (project_id, number);
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
    _SCHEMA_V11,
    _SCHEMA_V12,
    _SCHEMA_V13,
    _SCHEMA_V14,
    _SCHEMA_V15,
    _SCHEMA_V16,
    _SCHEMA_V17,
    _SCHEMA_V18,
    _SCHEMA_V19,
    _SCHEMA_V20,
    _SCHEMA_V21,
    _SCHEMA_V22,
    _SCHEMA_V23,
    _SCHEMA_V24,
    _SCHEMA_V25,
    _SCHEMA_V26,
    _SCHEMA_V27,
    _SCHEMA_V28,
    _SCHEMA_V29,
    _SCHEMA_V30,
    _SCHEMA_V31,
    _SCHEMA_V32,
    _SCHEMA_V33,
    _SCHEMA_V34,
    _SCHEMA_V35,
    _SCHEMA_V36,
    _SCHEMA_V37,
    _SCHEMA_V38,
    _SCHEMA_V39,
)
SCHEMA_VERSION = len(_MIGRATIONS)


class _Products(NamedTuple):
    """What one step of the ladder leaves in the store, looked for by name."""

    objects: tuple[str, ...] = ()
    """The tables and indexes its script creates, each ``IF NOT EXISTS``."""
    columns: tuple[tuple[str, str, str], ...] = ()
    """``(table, column, declaration)``, added after the script by
    :func:`_add_column_if_absent`, since a script cannot add a column
    conditionally."""


#: What each step from v15 on produces, keyed like :data:`_PREPARE` by the version
#: upgraded FROM (14 is v15). The ladder adds the columns (:func:`_run_step`), and
#: :func:`_converge_by_presence` looks for everything here whatever
#: ``user_version`` says (the note above _SCHEMA_V15 says why). v23 is a repair
#: and produces nothing. A later step that creates or adds anything needs an
#: entry, and a test compares every entry with what its step builds.
_PRODUCTS: dict[int, _Products] = {
    14: _Products(
        ("claude_account", "claude_account_alias", "claude_account_default", "project_setting"),
        (("fleet_agent", "account_slot", "INTEGER"),),
    ),
    15: _Products(
        ("claude_usage", "claude_usage_slot_time"),
        (("team_session", "limit_resets_at", "TEXT"),),
    ),
    16: _Products(columns=(("project", "onboarded_at", "TEXT"),)),
    17: _Products(("ui_state",), (("fleet_agent", "launch_spec", "TEXT"),)),
    18: _Products(("project_explainability",)),
    19: _Products(
        ("project_group",),
        (
            ("project", "group_id", "TEXT REFERENCES project_group (id)"),
            ("project", "position", "INTEGER"),
            ("project", "pinned_at", "TEXT"),
        ),
    ),
    20: _Products(("project_destination",)),
    21: _Products(("pending_revocation",)),
    23: _Products(columns=(("project_explainability", "api_url", "TEXT"),)),
    24: _Products(
        columns=(("fleet_agent", "socket_path", "TEXT"), ("fleet_agent", "boot_id", "TEXT"))
    ),
    25: _Products(("team_event_project_created",), (("fleet_agent", "server_started_at", "TEXT"),)),
    26: _Products(columns=(("project", "nickname", "TEXT"),)),
    27: _Products(columns=(("claude_usage", "token_fingerprint", "TEXT"),)),
    28: _Products(columns=(("team_task", "number", "INTEGER"),)),
    29: _Products(("team_task_number", "team_task_unnumbered")),
    30: _Products(columns=(("fleet_agent", "branch", "TEXT"),)),
    31: _Products(columns=(("fleet_agent", "board", "TEXT"),)),
    32: _Products(
        columns=(("team_session", "persona", "TEXT"), ("fleet_agent", "persona", "TEXT"))
    ),
    33: _Products(columns=(("fleet_agent", "ended_by", "TEXT"),)),
    34: _Products(("usage_session", "usage_session_project", "usage_turn", "usage_turn_session")),
    35: _Products(
        columns=(
            ("team_session", "prompted_at", "TEXT"),
            ("fleet_agent", "first_prompt", "TEXT"),
            ("fleet_agent", "first_prompt_at", "TEXT"),
        )
    ),
    36: _Products(columns=(("team_event", "actor", "TEXT"), ("team_event", "meta", "TEXT"))),
    37: _Products(
        columns=(
            ("project_group", "parent_project_id", "TEXT"),
            ("project_group", "route", "TEXT"),
            ("project_group", "max_agents", "INTEGER"),
            ("team_task", "origin_task_id", "TEXT"),
            ("team_task", "origin_project_id", "TEXT"),
        )
    ),
    38: _Products(("brief", "brief_number")),
}

_PROJECT_COLUMNS = (
    "id, root, linked_repos, codename, onboarded_at, group_id, position, pinned_at, nickname"
)
_GROUP_COLUMNS = "id, name, position, pinned_at, collapsed, created_at"
_DESTINATION_COLUMNS = (
    "project_id, api_url, environment, workspace_id, workspace_uid, workspace_name, "
    "studio_id, studio_uid, studio_name, key_uid, set_at, set_by"
)
"""A revival's SET for the arrangement (#140): a live row keeps its place, a tombstone
comes back loose and unpinned (:meth:`SqliteStore.ensure_project` says why)."""

_COLUMNS = "id, pool, project_id, text, tags, source, created_at, updated_at, deleted_at"
_PROMPT_COLUMNS = "id, project_id, text, source, created_at"
_METRIC_FIELDS = (
    "trace_id",
    "project_id",
    "session_id",
    "started_at",
    "ended_at",
    "wall_ms",
    "run_id",
    "run_kind",
    "opaque_config_id",
    "trigger",
    "client_reason",
    "status",
    "action",
    "query_id",
    "briefing_id",
    "config_fingerprint",
    "input_checkpoint",
    "resolved_scope_version",
    "round_trip_ms",
    "server_ms",
    "deadline_breached",
    "token_count",
    "items_count",
    "cache_status",
    "error_codes",
    "rendered_chars",
    "injected_chars",
    "frame_version",
    "instruction_version",
    "redaction_level",
    "snapshot_ref",
    "snapshot_untracked_excluded",
    "tokens_in",
    "tokens_out",
    "tool_calls",
    "delivery_source",
)
_METRIC_COLUMNS = ", ".join(_METRIC_FIELDS)

MAX_OPEN_TURN = timedelta(hours=24)
"""How old an open turn row may be and still be closed by a ``Stop``.

An orphaned row — a killed terminal, an ``open_turn`` that failed — would
otherwise absorb the whole gap to the next ``Stop`` as a 72-hour turn and land
in the wall-clock median. Older than this it stays open and is excluded
instead, which is what an unfinished turn is."""
_SESSION_COLUMNS = (
    "id, project_id, role, label, focus, started_at, last_seen_at, ended_at, cursor, state, "
    "transcript_path, account, model, effort, limit_resets_at, persona, prompted_at"
)
_USAGE_COLUMNS = (
    "slot, fetched_at, session_percent, session_resets_at, week_percent, week_resets_at, "
    "token_fingerprint"
)
#: How long usage readings are kept — long enough for a weekly window's history.
_USAGE_RETENTION = timedelta(days=7)
_TASK_COLUMNS = (
    "id, project_id, number, key, title, detail, status, role, needs, "
    "claimed_by, claim_expires_at, created_by, created_at, updated_at"
)
_EVENT_COLUMNS = (
    "seq, id, project_id, session_id, kind, text, task_id, to_role, created_at, actor, meta"
)
_FLEET_AGENT_FIELDS = (
    "id", "project_id", "label", "role", "binary", "tmux_socket", "pane_id", "session_id",
    "cwd", "worktree", "task_id", "spawned_by", "created_at", "ended_at", "exit_status",
    "account_slot", "launch_spec", "socket_path", "boot_id", "server_started_at", "branch",
    "board", "persona", "ended_by", "first_prompt", "first_prompt_at",
)  # fmt: skip
_FLEET_AGENT_COLUMNS = ", ".join(_FLEET_AGENT_FIELDS)


def _read_list(fields: Sequence[str], reads: Mapping[str, str]) -> str:
    """``fields`` as a SELECT list, in their order: each as stored, or as its expression
    in ``reads`` under its own name.

    One tuple for the columns a row is written with and read back as. Derived from the
    written list's text, the read list depended on ``board`` being its last name, and the
    next column appended after it would have made every fleet read invalid SQL (review of
    #259, round 27, 7/7)."""
    return ", ".join(f"{reads[field]} AS {field}" if field in reads else field for field in fields)


#: A row's ``board`` as it is read: one it never recorded is its session's, as the v32
#: backfill gives it (:data:`_UNRECORDED_BOARD`).
_FLEET_AGENT_BOARD = f"COALESCE(board, {_UNRECORDED_BOARD})"
#: :data:`_FLEET_AGENT_BOARD` for a store without ``team_session`` (doctor's damaged-store
#: case): no session to ask, so the project's, as the backfill gives it there.
_FLEET_AGENT_BOARD_SESSIONLESS = "COALESCE(board, project_id)"
#: :data:`_FLEET_AGENT_COLUMNS` as a row is read (:data:`_FLEET_AGENT_BOARD`).
_FLEET_AGENT_READ = _read_list(_FLEET_AGENT_FIELDS, {"board": _FLEET_AGENT_BOARD})
_FLEET_AGENT_READ_SESSIONLESS = _read_list(
    _FLEET_AGENT_FIELDS, {"board": _FLEET_AGENT_BOARD_SESSIONLESS}
)
_CLAUDE_ACCOUNT_COLUMNS = "slot, config_dir, alias, position, is_default, disabled, created_at"


class AmbiguousIdError(LookupError):
    """Raised when a partial entry id matches more than one entry."""

    def __init__(self, ref: str) -> None:
        super().__init__(f"entry id {ref!r} is ambiguous")
        self.ref = ref


def is_the_home(root: Path) -> bool:
    """Whether ``root`` is the aisquare home itself (resolved, so a symlink is the same home).

    A root that is a symlink loop is not the home: ``resolve`` raises ``RuntimeError`` for
    one on 3.11 and 3.12 (``OSError`` from 3.13), and ``project prune`` asks this of every
    row, where a loop is a root it must report, never a traceback."""
    try:
        return root.resolve() == paths.aisquare_home().resolve()
    except (OSError, RuntimeError):
        return False


class ContextStore(Protocol):
    """Everything the service layer needs from persistent context storage."""

    def add(self, entry: ContextEntry) -> ContextEntry: ...
    def get(self, ref: str) -> ContextEntry | None: ...
    def entries(
        self, pool: Pool | None = None, *, project_id: str | None = None
    ) -> list[ContextEntry]: ...
    def has_entries(self, project_id: str) -> bool: ...
    def search(
        self, query: str, *, pool: Pool | None = None, project_id: str | None = None
    ) -> list[ContextEntry]: ...
    def update(
        self, entry_id: str, *, text: str | None = None, tags: list[str] | None = None
    ) -> ContextEntry: ...
    def delete(self, entry_id: str) -> None: ...
    def promote(self, entry_id: str) -> ContextEntry: ...
    def ensure_project(self, project: ProjectInfo) -> None: ...
    def onboard_project(
        self, project: ProjectInfo, *, codename: str | None = None, explicit: bool = False
    ) -> ProjectInfo: ...
    def list_projects(
        self, *, all: bool = False, include_forgotten: bool = False
    ) -> list[ProjectInfo]: ...
    def captured_projects(self) -> list[ProjectInfo]: ...
    def get_project(
        self, project_id: str, *, include_forgotten: bool = False
    ) -> ProjectInfo | None: ...
    def forgotten_among(self, project_ids: Collection[str]) -> set[str]: ...
    def find_projects(self, term: str) -> list[ProjectInfo]: ...
    def add_linked_repo(self, project_id: str, repo: str) -> ProjectInfo: ...
    def forget_project(self, project_id: str) -> None: ...
    def purge_project(self, project_id: str) -> dict[str, int]: ...
    def project_activity(self) -> dict[str, str]: ...
    def add_prompt(
        self, text: str, project_id: str | None, source: str = "claude-code"
    ) -> PromptRecord: ...
    def recent_prompts(
        self, project_id: str | None = None, *, limit: int = 20
    ) -> list[PromptRecord]: ...
    def team_active(self, project_id: str) -> bool: ...
    def upsert_session(self, session: TeamSession) -> TeamSession: ...
    def get_session(
        self, session_id: str, *, project_ids: Collection[str] | None = None
    ) -> TeamSession | None: ...
    def team_sessions(self, project_id: str) -> list[TeamSession]: ...
    def team_sessions_seen_since(
        self, project_id: str, since: datetime | None, *, ids: Sequence[str] = ()
    ) -> list[TeamSession]: ...
    def update_session(
        self,
        session_id: str,
        *,
        role: str | None = None,
        label: str | None = None,
        focus: str | None = None,
    ) -> TeamSession: ...
    def set_session_persona(self, session_id: str, persona: str | None) -> TeamSession: ...
    def touch_session(
        self, session_id: str, *, cursor: int | None = None, state: str | None = None
    ) -> None: ...
    def replace_session_state(self, session_id: str, expected: str, state: str) -> bool: ...
    def mark_handover(self, session_id: str, *, held_after: datetime) -> bool: ...
    def mark_attention(self, session_id: str) -> bool: ...
    def mark_limited(self, session_id: str, resets_at: datetime | None) -> None: ...
    def set_transcript_path(self, session_id: str, transcript_path: str) -> None: ...
    def unmark_handover(self, session_id: str, state: str) -> None: ...
    def end_session(self, session_id: str, *, release_claims: bool = True) -> list[TeamTask]: ...
    def release_claims(self, session_id: str) -> list[TeamTask]: ...
    def upsert_task(self, task: TeamTask) -> tuple[TeamTask, bool]: ...
    def get_task(
        self, ref: str, *, project_ids: Collection[str] | None = None
    ) -> TeamTask | None: ...
    def task_by_number(self, project_id: str, number: int) -> TeamTask | None: ...
    def task_numbers(self, project_id: str) -> dict[str, int]: ...
    def team_tasks(
        self, project_id: str, *, status: TaskStatus | None = None
    ) -> list[TeamTask]: ...
    def has_team_task_in(self, project_id: str, statuses: Collection[TaskStatus]) -> bool: ...
    def claim_task(self, task_id: str, session_ref: str, lease_until: datetime) -> bool: ...
    def renew_leases(self, session_id: str, lease_until: datetime) -> None: ...
    def set_task_status(self, task_id: str, status: TaskStatus) -> TeamTask: ...
    def release_task(self, task_id: str) -> TeamTask: ...
    def reopen_task(self, task_id: str) -> TeamTask: ...
    def next_task(
        self,
        project_id: str,
        *,
        role: str | None = None,
        status: TaskStatus = "todo",
        prefer: str | None = None,
        only: str | None = None,
    ) -> TeamTask | None: ...
    def task_statuses(self, project_id: str) -> dict[str, str]: ...
    def adopt_fleet_agent_session(
        self, agent_id: str, previous: str | None, session_id: str, lease_until: datetime
    ) -> bool: ...
    def retire_fleet_assignments(self, task_id: str) -> int: ...
    def open_turn(self, metric: TurnMetric) -> TurnMetric: ...
    def close_turn(self, session_id: str, *, ended_at: datetime) -> TurnMetric | None: ...
    def turn_metrics(
        self, *, project_id: str | None = None, session_id: str | None = None, limit: int = 500
    ) -> list[TurnMetric]: ...
    def session_usage(self, session_id: str) -> SessionUsage | None: ...
    def record_usage(
        self,
        session: SessionUsage,
        turn: TurnUsage | None,
        *,
        previous_state: str | None,
        metric_counts: tuple[int, int, int] | None = None,
    ) -> bool: ...
    def session_usages(
        self,
        *,
        project_id: str | None = None,
        session_id: str | None = None,
        limit: int = 10_000,
    ) -> list[SessionUsage]: ...
    def turn_usages(self, session_id: str) -> list[TurnUsage]: ...
    def get_meta(self, key: str) -> str | None: ...
    def set_meta(self, key: str, value: str) -> None: ...
    def delete_meta(self, key: str) -> None: ...
    def list_meta(self, prefix: str) -> dict[str, str]: ...
    def add_signal_events(
        self, writes: Sequence[tuple[TeamEvent, str, dict[str, Any]]]
    ) -> list[TeamEvent]: ...
    def add_team_event(self, event: TeamEvent) -> TeamEvent: ...
    def add_brief(
        self, brief_id: str, project_id: str, data: str, created_at: datetime, event: TeamEvent
    ) -> tuple[int, TeamEvent]: ...
    def get_brief(self, project_id: str, ref: str) -> BriefRow | None: ...
    def briefs(self, project_id: str) -> list[BriefRow]: ...
    def all_brief_documents(self) -> list[str]: ...
    def complete_task_if_unchanged(self, task_id: str, briefs: Mapping[str, int]) -> TeamTask: ...
    def save_brief(
        self,
        brief_id: str,
        project_id: str,
        *,
        expected: int,
        revision: int,
        data: str,
        updated_at: datetime,
        event: TeamEvent,
        transitions: Sequence[TaskTransition] = (),
    ) -> list[TeamEvent]: ...
    def get_event(self, event_id: str) -> TeamEvent | None: ...
    def get_event_by_seq(self, seq: int) -> TeamEvent | None: ...
    def find_event_by_id(self, ref: str) -> TeamEvent | None: ...
    def events_since(
        self,
        project_id: str,
        seq: int,
        *,
        exclude_session: str | None = None,
        exclude_kinds: Collection[str] = (),
        exclude_actor: str | None = None,
        relevant: Relevance | None = None,
        limit: int = 50,
    ) -> list[TeamEvent]: ...
    def recent_events(
        self,
        project_id: str,
        *,
        exclude_kinds: Collection[str] = (),
        relevant: Relevance | None = None,
        limit: int = 10,
    ) -> list[TeamEvent]: ...
    def filtered_events(
        self,
        project_id: str,
        *,
        session_id: str | None = None,
        since_iso: str | None = None,
        since_seq: int | None = None,
        kind: str | None = None,
        task_id: str | None = None,
        exclude_kinds: Collection[str] = (),
        limit: int = 30,
    ) -> list[TeamEvent]: ...
    def team_events_since(
        self,
        project_id: str,
        since: datetime,
        *,
        kinds: Sequence[str],
        human_kinds: Sequence[str] = (),
        session_kinds: Sequence[str] = (),
        sessions: Sequence[str] = (),
    ) -> list[TeamEvent]: ...
    def newest_session_event(
        self, project_id: str, session_id: str, kind: str, *, since: datetime
    ) -> TeamEvent | None: ...
    def latest_seq(self, project_id: str) -> int: ...
    def terminal_events(self, project_id: str) -> dict[str, TeamEvent]: ...
    def set_codename(self, project_id: str, codename: str) -> ProjectInfo: ...
    def set_project_nickname(self, project_id: str, nickname: str | None) -> ProjectInfo: ...
    def codenames_in_use(self) -> set[str]: ...
    def upsert_fleet_agent(self, agent: FleetAgent) -> FleetAgent: ...
    def upsert_fleet_agent_unless_stopped(
        self, agent: FleetAgent, replaces: str
    ) -> FleetAgent | None: ...
    def mark_prompted(
        self, session_id: str, at: datetime, *, agent_id: str | None = None
    ) -> bool: ...
    def mark_first_prompt_unsent(self, agent_id: str, at: datetime) -> bool: ...
    def take_first_prompt(
        self, at: datetime, *, session_id: str, agent_id: str | None = None
    ) -> bool: ...
    def first_prompt_rows(self, since: datetime) -> list[FleetAgent]: ...
    def get_fleet_agent(self, ref: str) -> FleetAgent | None: ...
    def fleet_agents(self, project_id: str, *, live_only: bool = False) -> list[FleetAgent]: ...
    def live_fleet_agents(self) -> list[FleetAgent]: ...
    def fleet_label_heads(self, project_id: str | None = None) -> list[FleetAgent]: ...
    def fleet_agents_ended_since(self, project_id: str, since: datetime) -> list[FleetAgent]: ...
    def fleet_live_counts(self, since: datetime) -> dict[str, int]: ...
    def fleet_agent_boards(self, project_id: str, *, live_only: bool = False) -> list[str]: ...
    def fleet_board(self, project_id: str) -> str | None: ...
    def fleet_manager_board(self, project_id: str) -> str | None: ...
    def fleet_agent_for_session(self, session_id: str) -> FleetAgent | None: ...
    def ui_state(self, key: str) -> str | None: ...
    def set_ui_state(self, key: str, value: str | None) -> None: ...
    def replace_ui_state(self, key: str, expected: str | None, value: str | None) -> bool: ...
    def project_groups(self) -> list[ProjectGroup]: ...
    def get_project_group(self, group_id: str) -> ProjectGroup | None: ...
    def find_project_group(self, name: str) -> ProjectGroup | None: ...
    def create_project_group(self, name: str, *, group_id: str | None = None) -> ProjectGroup: ...
    def update_project_group(
        self,
        group_id: str,
        *,
        name: str | None = None,
        position: int | None = None,
        pinned_at: datetime | EllipsisType | None = ...,
        collapsed: bool | None = None,
    ) -> ProjectGroup: ...
    def delete_project_group(self, group_id: str) -> list[str]: ...
    def group_settings(self, group_id: str) -> GroupSettings: ...
    def set_group_settings(
        self,
        group_id: str,
        *,
        parent_project_id: str | EllipsisType | None = ...,
        route: Mapping[str, object] | EllipsisType | None = ...,
        max_agents: int | EllipsisType = ...,
    ) -> GroupSettings: ...
    def set_task_origin(
        self, task_id: str, origin_task_id: str, origin_project_id: str
    ) -> None: ...
    def task_origins(self, project_id: str) -> dict[str, tuple[str, str]]: ...
    def linked_tasks(self, origin_task_id: str) -> list[TeamTask]: ...
    def linked_counts(self, origin_project_id: str) -> dict[str, tuple[int, int]]: ...
    def layout_change(self) -> contextlib.AbstractContextManager[None]: ...
    def update_project_layout(
        self,
        project_id: str,
        *,
        group_id: str | EllipsisType | None = ...,
        position: int | EllipsisType | None = ...,
        pinned_at: datetime | EllipsisType | None = ...,
    ) -> ProjectInfo: ...
    def project_explainability(self, project_id: str) -> ProjectExplainability | None: ...
    def project_explainability_all(self) -> list[ProjectExplainability]: ...
    def set_project_explainability(
        self,
        project_id: str,
        *,
        target: str,
        key_path: Path,
        set_by: str | None,
        minted: str | None = None,
        api_url: str | None = None,
    ) -> ProjectExplainability: ...
    def clear_project_explainability(self, project_id: str) -> bool: ...
    # Where a project's traces land (v21, #142).
    def project_destination(self, project_id: str) -> TraceDestination | None: ...
    def project_destinations(self) -> list[TraceDestination]: ...
    def set_project_destination(self, destination: TraceDestination) -> TraceDestination: ...
    def set_project_destination_key(self, project_id: str, key_uid: str | None) -> None: ...
    def detach_minted_key(self, project_id: str) -> tuple[bool, ProjectExplainability | None]: ...
    def clear_project_destination(self, project_id: str) -> bool: ...
    # The revocations owed for keys the CLI minted (v22, #142).
    def pending_revocations(self) -> list[PendingRevocation]: ...
    def settle_revocation(self, key_uid: str) -> bool: ...
    def note_revocation_failure(self, key_uid: str, reason: str) -> None: ...
    def fleet_agent_by_label(
        self, project_id: str, label: str, *, live_only: bool = True
    ) -> FleetAgent | None: ...
    def end_fleet_agent(
        self, agent_id: str, *, exit_status: int | None = None, ended_by: EndedBy
    ) -> FleetAgent: ...
    def set_fleet_agent_persona(self, agent_id: str, persona: str | None) -> FleetAgent: ...
    def end_fleet_agent_if_live(
        self, agent_id: str, *, exit_status: int | None = None, ended_by: EndedBy
    ) -> FleetAgent | None: ...
    def mark_fleet_agent_stopped(self, agent_id: str) -> FleetAgent: ...
    def forget_fleet_agent_server_start(self, agent_id: str, started: datetime) -> bool: ...
    def record_fleet_agent_branch(self, agent_id: str, branch: str | None) -> None: ...
    # The Claude account registry (v15, #145): the arrangement of the slots.
    def claude_accounts(self) -> list[ClaudeAccountRecord]: ...
    def upsert_claude_account(self, slot: int, config_dir: Path) -> ClaudeAccountRecord: ...
    def delete_claude_account(self, slot: int) -> bool: ...
    def delete_claude_accounts(self, slots: Sequence[int]) -> int: ...
    def set_claude_account_alias(self, slot: int, alias: str | None) -> None: ...
    def set_claude_account_disabled(self, slot: int, disabled: bool) -> None: ...
    def order_claude_accounts(self, slots: Sequence[int]) -> None: ...
    def promote_retired_claude_account_default(self) -> int | None: ...
    # Usage readings (v16, #146): the history behind "how fast is this window filling".
    def add_usage_sample(self, sample: UsageSample) -> None: ...
    def usage_samples(self, slot: int, *, since: datetime) -> list[UsageSample]: ...
    def delete_usage_samples(self, slot: int) -> int: ...
    # Per-project settings (v15): one key, one value, per project.
    def project_setting(self, project_id: str, key: str) -> str | None: ...
    def set_project_setting(self, project_id: str, key: str, value: str) -> None: ...
    def clear_project_setting(self, project_id: str, key: str) -> bool: ...
    def project_settings(self, key: str) -> dict[str, str]: ...
    # What the open could not converge (doctor's database row).
    def missing_schema(self) -> list[str]: ...
    def has_table(self, table: str) -> bool: ...
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
    onboarded = row["onboarded_at"]
    return ProjectInfo(
        id=row["id"],
        root=Path(row["root"]),
        linked_repos=json.loads(row["linked_repos"]),
        codename=row["codename"],
        onboarded_at=datetime.fromisoformat(onboarded) if onboarded else None,
        group_id=row["group_id"],
        position=row["position"],
        pinned_at=_maybe_dt(row["pinned_at"]),
        nickname=row["nickname"],
    )


def _row_to_project_group(row: sqlite3.Row) -> ProjectGroup:
    return ProjectGroup(
        id=row["id"],
        name=row["name"],
        position=int(row["position"]),
        pinned_at=_maybe_dt(row["pinned_at"]),
        collapsed=bool(row["collapsed"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_fleet_agent(row: sqlite3.Row) -> FleetAgent:
    return FleetAgent(
        id=row["id"],
        project_id=row["project_id"],
        label=row["label"],
        role=row["role"],
        binary=row["binary"],
        tmux_socket=row["tmux_socket"],
        pane_id=row["pane_id"],
        session_id=row["session_id"],
        cwd=Path(row["cwd"]),
        worktree=bool(row["worktree"]),
        task_id=row["task_id"],
        spawned_by=row["spawned_by"],
        created_at=datetime.fromisoformat(row["created_at"]),
        ended_at=_maybe_dt(row["ended_at"]),
        exit_status=row["exit_status"],
        account_slot=row["account_slot"],
        launch_spec=_launch_spec(row["launch_spec"]),
        socket_path=Path(row["socket_path"]) if row["socket_path"] else None,
        boot_id=row["boot_id"],
        server_started_at=_maybe_dt(row["server_started_at"]),
        branch=row["branch"],
        board=row["board"],  # never NULL as read (`_FLEET_AGENT_READ`)
        persona=row["persona"],
        ended_by=row["ended_by"],
        first_prompt=row["first_prompt"],
        first_prompt_at=_maybe_dt(row["first_prompt_at"]),
    )


def _row_to_trace_destination(row: sqlite3.Row) -> TraceDestination:
    return TraceDestination(
        project_id=row["project_id"],
        api_url=row["api_url"],
        environment=row["environment"],
        workspace_id=int(row["workspace_id"]),
        workspace_uid=row["workspace_uid"],
        workspace_name=row["workspace_name"],
        studio_id=int(row["studio_id"]) if row["studio_id"] is not None else None,
        studio_uid=row["studio_uid"],
        studio_name=row["studio_name"],
        key_uid=row["key_uid"],
        set_at=datetime.fromisoformat(row["set_at"]),
        set_by=row["set_by"],
    )


def _row_to_project_explainability(row: sqlite3.Row) -> ProjectExplainability:
    return ProjectExplainability(
        project_id=row["project_id"],
        target=row["target"],
        key_path=Path(row["key_path"]),
        set_at=datetime.fromisoformat(row["set_at"]),
        set_by=row["set_by"],
        api_url=row["api_url"],
    )


def _launch_spec(raw: object) -> LaunchSpec | None:
    """The recorded spec, or ``None`` for a row without one or with one that no longer parses."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return LaunchSpec.model_validate_json(raw)
    except ValueError:
        return None


def _row_to_claude_account(row: sqlite3.Row) -> ClaudeAccountRecord:
    return ClaudeAccountRecord(
        slot=int(row["slot"]),
        config_dir=Path(row["config_dir"]),
        alias=row["alias"],
        position=int(row["position"]),
        is_default=bool(row["is_default"]),
        disabled=bool(row["disabled"]),
        created_at=datetime.fromisoformat(row["created_at"]),
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


_SESSION_USAGE_COLUMNS = (
    "session_id, project_id, transcript_path, state, usage, turns, total_tokens, weighted, "
    "cost_usd, started_at, updated_at, ended_at"
)
_TURN_USAGE_COLUMNS = (
    "id, session_id, project_id, trace_id, seq, trigger, recorded_at, usage, total_tokens, "
    "weighted, cost_usd, scan_ms"
)


def _like_prefix(prefix: str) -> str:
    """``prefix`` as a LIKE pattern matching itself and anything after it, literally."""
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%"


def _json_object(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _row_to_session_usage(row: sqlite3.Row) -> SessionUsage:
    return SessionUsage(
        session_id=row["session_id"],
        project_id=row["project_id"],
        transcript_path=row["transcript_path"],
        state=row["state"],
        usage=_json_object(row["usage"]),
        turns=row["turns"],
        total_tokens=row["total_tokens"],
        weighted=row["weighted"],
        cost_usd=row["cost_usd"],
        started_at=datetime.fromisoformat(row["started_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        ended_at=_maybe_dt(row["ended_at"]),
    )


def _row_to_turn_usage(row: sqlite3.Row) -> TurnUsage:
    return TurnUsage(
        id=row["id"],
        session_id=row["session_id"],
        project_id=row["project_id"],
        trace_id=row["trace_id"],
        seq=row["seq"],
        trigger=row["trigger"],
        recorded_at=datetime.fromisoformat(row["recorded_at"]),
        usage=_json_object(row["usage"]),
        total_tokens=row["total_tokens"],
        weighted=row["weighted"],
        cost_usd=row["cost_usd"],
        scan_ms=row["scan_ms"],
    )


def _row_to_metric(row: sqlite3.Row) -> TurnMetric:
    def flag(name: str) -> bool | None:
        value = row[name]
        return None if value is None else bool(value)

    codes = json.loads(row["error_codes"] or "[]")
    return TurnMetric(
        trace_id=row["trace_id"],
        project_id=row["project_id"],
        session_id=row["session_id"],
        started_at=datetime.fromisoformat(row["started_at"]),
        ended_at=_maybe_dt(row["ended_at"]),
        wall_ms=row["wall_ms"],
        run_id=row["run_id"],
        run_kind=row["run_kind"],
        opaque_config_id=row["opaque_config_id"],
        trigger=row["trigger"],
        client_reason=row["client_reason"],
        status=row["status"],
        action=row["action"],
        query_id=row["query_id"],
        briefing_id=row["briefing_id"],
        config_fingerprint=row["config_fingerprint"],
        input_checkpoint=row["input_checkpoint"],
        resolved_scope_version=row["resolved_scope_version"],
        round_trip_ms=row["round_trip_ms"],
        server_ms=row["server_ms"],
        deadline_breached=flag("deadline_breached"),
        token_count=row["token_count"],
        items_count=row["items_count"],
        cache_status=row["cache_status"],
        error_codes=codes if isinstance(codes, list) else [],
        rendered_chars=row["rendered_chars"],
        injected_chars=row["injected_chars"],
        frame_version=row["frame_version"],
        instruction_version=row["instruction_version"],
        redaction_level=row["redaction_level"],
        snapshot_ref=row["snapshot_ref"],
        snapshot_untracked_excluded=flag("snapshot_untracked_excluded"),
        tokens_in=row["tokens_in"],
        tokens_out=row["tokens_out"],
        tool_calls=row["tool_calls"],
        delivery_source=row["delivery_source"],
    )


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
        limit_resets_at=_maybe_dt(row["limit_resets_at"]),
        persona=row["persona"],
        prompted_at=_maybe_dt(row["prompted_at"]),
    )


def _row_to_usage_sample(row: sqlite3.Row) -> UsageSample:
    return UsageSample(
        slot=int(row["slot"]),
        fetched_at=datetime.fromisoformat(row["fetched_at"]),
        session_percent=row["session_percent"],
        session_resets_at=_maybe_dt(row["session_resets_at"]),
        week_percent=row["week_percent"],
        week_resets_at=_maybe_dt(row["week_resets_at"]),
        token_fingerprint=row["token_fingerprint"],
    )


def _row_to_task(row: sqlite3.Row) -> TeamTask:
    return TeamTask(
        id=row["id"],
        project_id=row["project_id"],
        number=row["number"],
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


class BriefRow(NamedTuple):
    """One stored brief: its document, and what the store keeps beside it."""

    id: str
    number: int
    revision: int
    data: str


TaskTransition = tuple[str, Literal["reopen", "block"], str]
"""A board task a brief write moves: ``(task id, what, the task event's text)``
(:meth:`SqliteStore.save_brief`)."""

_BRIEF_COLUMNS = "id, number, revision, data"
_BRIEF_NUMBER_REF = re.compile(r"^[#Bb]?([0-9]{1,6})$")
"""A brief ref that is its board number: ``3``, ``#3`` or ``B3``."""


class BriefChangedError(RuntimeError):
    """A brief write lost to another: the revision it read is not the stored one."""

    def __init__(self, brief_id: str) -> None:
        super().__init__(f"brief {brief_id} changed while this write was being made; retry it")
        self.brief_id = brief_id


def _row_to_brief(row: sqlite3.Row) -> BriefRow:
    return BriefRow(
        id=str(row["id"]), number=int(row["number"]), revision=int(row["revision"]),
        data=str(row["data"]),
    )  # fmt: skip


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
        actor=row["actor"],
        meta=json.loads(row["meta"]) if row["meta"] else None,
    )


def unmet_needs(task: TeamTask, statuses: Mapping[str, str]) -> list[str]:
    """The dependencies of ``task`` that are not resolved yet.

    A need is satisfied once its task is ``done`` or ``dropped``; anything
    else (including an unknown id) keeps the dependent task waiting.
    """
    return [need for need in task.needs if statuses.get(need) not in CLOSED_STATUSES]


def _fts_match(query: str) -> str:
    """Turn free text into a safe FTS5 query: prefix-match every word token.

    Building the MATCH expression ourselves (rather than passing raw user input)
    keeps arbitrary punctuation from being parsed as FTS5 operators.
    """
    tokens = re.findall(r"[A-Za-z0-9]+", query)
    return " OR ".join(f"{token}*" for token in tokens)


#: The project rows a read may see. ``project forget`` tombstones a registration
#: (``forgotten_at``, v14) and promises its rows stay hidden until the root is
#: registered again; the row is the one place the tombstone lives, so every
#: project-scoped read of another table qualifies its project id by it.
_VISIBLE_PROJECTS = "(SELECT id FROM project WHERE forgotten_at IS NULL)"

#: ``team_meta`` rows are matched one ``OR`` term per key, and SQLite parses a
#: chain of them into a tree as deep as it is long, refusing one past 1000
#: levels (``Expression tree is too large``): 500 recorded sessions made 1,002
#: terms and the purge rolled back. Session keys go in statements of this many.
_META_BATCH = 100


def _scope_filter(
    pool: Pool | None, project_id: str | None, *, prefix: str = ""
) -> tuple[list[str], list[str]]:
    """Build the pool/project ``WHERE`` clauses and params for a query.

    With no ``pool`` but a ``project_id``, this is the in-scope view that
    unqualified ``entries``/``search`` show: the user pool plus that project's
    pool. ``prefix`` qualifies the column names for joined queries (e.g.
    ``"e."``).

    A forgotten project's rows are out of scope however they are asked for. The
    cwd still resolves to the project's id after ``project forget``, so without
    :data:`_VISIBLE_PROJECTS` here the "hidden until registered again" promise
    held for ``project list`` and not for ``context list`` or ``inject``.
    """
    pool_col, pid_col = f"{prefix}pool", f"{prefix}project_id"
    visible = f"{pid_col} IN {_VISIBLE_PROJECTS}"
    if pool == "user":
        return [f"{pool_col} = 'user'"], []
    if pool == "project":
        return [f"{pool_col} = 'project' AND {pid_col} = ? AND {visible}"], [project_id or ""]
    if project_id is not None:
        in_scope = (
            f"({pool_col} = 'user' OR ({pool_col} = 'project' AND {pid_col} = ? AND {visible}))"
        )
        return [in_scope], [project_id]
    return [f"{pool_col} = 'user'"], []


#: When each table's rows last touched a project, keyed by the table it reads
#: (:meth:`SqliteStore.project_activity`).
_ACTIVITY = {
    "entry": "SELECT project_id, updated_at FROM entry WHERE project_id IS NOT NULL",
    "prompt": "SELECT project_id, created_at FROM prompt WHERE project_id IS NOT NULL",
    "team_session": "SELECT project_id, last_seen_at FROM team_session",
    "team_task": "SELECT project_id, updated_at FROM team_task",
    "team_event": "SELECT project_id, created_at FROM team_event",
    "fleet_agent": "SELECT project_id, created_at FROM fleet_agent",
}


class SqliteStore:
    """The SQLite implementation of :class:`ContextStore`."""

    def __init__(self, connection: sqlite3.Connection, *, names: Collection[str]) -> None:
        """``names``: the store's tables and indexes, as :func:`_migrate` read them opening it."""
        self._conn = connection
        self._layout_depth = 0  # open :meth:`layout_change` blocks
        # The SELECT list of a ``fleet_agent`` read on this store. A row that recorded no board
        # reads its session's (:data:`_FLEET_AGENT_READ`), a subquery on ``team_session``,
        # which SQLite's COALESCE runs only for such a row. On a store without that table
        # (doctor's damaged-store case, which the v32 backfill tolerates) it made every fleet
        # read, the live-only ones doctor's own checks make included, fail with "no such
        # table: team_session" (review of #259, round 27, 5/7). Told by the names the open
        # read, not asked of ``sqlite_master`` again (round 28, 9/10).
        self._names = frozenset(names)
        sessions = "team_session" in names
        self._fleet_agent_read = _FLEET_AGENT_READ if sessions else _FLEET_AGENT_READ_SESSIONLESS
        self._fleet_agent_board = _FLEET_AGENT_BOARD if sessions else _FLEET_AGENT_BOARD_SESSIONLESS

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

    def has_entries(self, project_id: str) -> bool:
        """Whether ``entries("project", project_id=project_id)`` would list any: one row asked
        for, none built. Doctor asks it of every captured row on each run, and loaded every
        entry of the rows that hold context to learn that one existed (review of #259,
        round 19)."""
        clauses, params = _scope_filter("project", project_id)
        where = " AND ".join(["deleted_at IS NULL", *clauses])
        found = self._conn.execute(f"SELECT 1 FROM entry WHERE {where} LIMIT 1", params)
        return found.fetchone() is not None

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
        """CAPTURE the project: make sure a live row exists, and never list it.

        This is what a hook recording a prompt and the MCP server's session
        call — automatic registration, so prompt history and injection work in
        every directory (#139) — and only while ``[projects] capture`` is on
        (``services.project.tracked``); off, the default since 0.8.1, nothing
        captures. It never sets ``onboarded_at``, so a captured
        directory stays off the list until something DELIBERATE registers it
        (:meth:`onboard_project`).

        It does clear ``forgotten_at``: the next prompt in a forgotten or
        pruned directory brings the row back CAPTURED, which is what makes a
        forget stick. A tombstone left in place would take every later prompt and
        turn metric into a row no read can reach: ``log`` would say nothing had
        been captured, and ``project list --all``, ``doctor``, ``prune
        --captured-only`` and ``metrics show --project`` would never see the
        directory again. Before #139 this revival also re-listed the project,
        which is how ``project forget`` came undone on the next prompt.

        Nothing else is written. A forget clears the mark and the place in the
        arrangement (#140), so a revived row comes back captured, loose and
        unpinned, and a live one keeps its own. The tombstones that older cuts
        left holding either were repaired once, by v23, rather than on every
        prompt (review of #168 at the fold).
        """
        self._conn.execute(
            "INSERT INTO project (id, root, name, linked_repos, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET forgotten_at = NULL",
            (
                project.id,
                str(project.root),
                project.root.name or str(project.root),
                json.dumps(project.linked_repos),
                _now_iso(),
            ),
        )
        self._conn.commit()

    def onboard_project(
        self, project: ProjectInfo, *, codename: str | None = None, explicit: bool = False
    ) -> ProjectInfo:
        """Register the project ON PURPOSE: shown from now on, and revived if forgotten.

        ``init``, ``project onboard`` / ``link`` / ``switch``, the sidebar's
        ``+``, ``team on``, a fleet spawn and a fact written by hand — the
        actions that mean "this is one of my projects".
        ``onboarded_at`` is set once and kept; ``forgotten_at`` is cleared, so
        the row comes back with whatever history it still carries (see v14).
        A revived row comes back loose and unpinned, as :meth:`ensure_project`
        says, and marked now: a tombstone holds no mark to keep (v23). A live
        one keeps its place.

        ``codename`` is given to a row that has none, in the same statement: a fleet spawn's,
        whose session and branch were named by it before anything was written. A row's own
        codename is kept; one another project holds raises ``sqlite3.IntegrityError`` and
        writes nothing.

        ``explicit`` is the owner's own "add this folder": ``init`` (the TUI's ``+`` runs it)
        and ``project onboard``. Only that makes the aisquare home one of their projects
        (the owner's decision D5, 2026-10-23); a forget hides it again, as any project.
        """
        if is_the_home(project.root) and not explicit:
            # The aisquare home's row is the captain's HOME BOARD (services.captain):
            # captured so it can hold the captain's row and events, never one of the
            # owner's projects — a spawn, a codename, a rename or a launch's
            # activation onboarding it would put `.aisquare` in the sidebar, in
            # `project list` and in the captain's own projects() tool (T2, 13121).
            # A spawn's codename is still given to it when it has none: a codename is
            # no onboarding, and the captain's tmux session was named by it before the
            # row was written (``fleet._add_named``, 0.8.x's capture-off rule).
            self.ensure_project(project)
            home = self._written_project(project.id)
            if codename is not None and home.codename is None:
                return self.set_codename(project.id, codename)
            return home
        now = _now_iso()
        self._conn.execute(
            "INSERT INTO project "
            "(id, root, name, linked_repos, created_at, onboarded_at, codename) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET forgotten_at = NULL, "
            "onboarded_at = COALESCE(project.onboarded_at, excluded.onboarded_at), "
            "codename = COALESCE(project.codename, excluded.codename)",
            (
                project.id,
                str(project.root),
                project.root.name or str(project.root),
                json.dumps(project.linked_repos),
                now,
                now,
                codename,
            ),
        )
        self._conn.commit()
        return self._written_project(project.id)

    def _written_project(self, project_id: str) -> ProjectInfo:
        """The row just written — a ``KeyError`` if it is gone already (another process's
        purge between the write and this read), as every lookup here says a missing
        project. Checked, not asserted: under ``python -O`` an ``assert`` is stripped and
        ``None`` went back against the return type (pre-gate review of #219)."""
        stored = self.get_project(project_id)
        if stored is None:
            raise KeyError(project_id)
        return stored

    def list_projects(
        self, *, all: bool = False, include_forgotten: bool = False
    ) -> list[ProjectInfo]:
        """Onboarded projects by name; ``all`` adds captures, ``include_forgotten`` every row.

        Two different questions. ``all`` is "every directory this machine has
        seen", not just "my projects": the sidebar, ``project list`` and the
        active-pin fallback read the default; machine-wide sweeps (reap,
        doctor's row checks, the baseline, a shutdown's pause scan) pass
        ``all=True`` because a captured directory can hold fleet rows, sessions
        and a pause like any other. Forgotten rows stay out, which is the
        promise :func:`forget_project` makes.

        ``include_forgotten=True`` reads past that promise too, for the one
        question a tombstone must not hide: a forgotten project can still hold
        LIVE ``fleet_agent`` rows (``forget`` reads liveness and writes the
        tombstone in separate statements, and a concurrent ``fleet spawn``
        revives the row through :meth:`onboard_project`), and those agents'
        panes are real processes. ``fleet shutdown`` asks this way so a
        tombstone cannot leave a row live on a socket it just took down, with
        nothing able to reconcile it. A forget clears ``onboarded_at``, so a
        tombstone is never an onboarded row: this returns the captured rows as
        well, and ``all`` adds nothing to it.
        """
        if include_forgotten:
            clause = ""
        elif all:
            clause = " WHERE forgotten_at IS NULL"
        else:
            clause = " WHERE forgotten_at IS NULL AND onboarded_at IS NOT NULL"
        rows = self._conn.execute(
            f"SELECT {_PROJECT_COLUMNS} FROM project{clause} ORDER BY name"
        ).fetchall()
        return [_row_to_project(row) for row in rows]

    def captured_projects(self) -> list[ProjectInfo]:
        """The registrations a session made on its own that nothing has added on purpose.

        Never the aisquare home's: the captain's home board is captured on purpose and never
        onboarded, and doctor counted it among the scratch directories to prune."""
        rows = self._conn.execute(
            f"SELECT {_PROJECT_COLUMNS} FROM project "
            "WHERE forgotten_at IS NULL AND onboarded_at IS NULL ORDER BY name"
        ).fetchall()
        return [project for project in map(_row_to_project, rows) if not is_the_home(project.root)]

    def get_project(
        self, project_id: str, *, include_forgotten: bool = False
    ) -> ProjectInfo | None:
        """The live row for ``project_id``; with ``include_forgotten``, its tombstone too, as
        :meth:`list_projects` reads one: a forgotten project's codename names the session its
        agents' windows linger in."""
        live = "" if include_forgotten else " AND forgotten_at IS NULL"
        row = self._conn.execute(
            f"SELECT {_PROJECT_COLUMNS} FROM project WHERE id = ?{live}", (project_id,)
        ).fetchone()
        return _row_to_project(row) if row is not None else None

    def forgotten_among(self, project_ids: Collection[str]) -> set[str]:
        """Which of ``project_ids`` the store holds forgotten (a tombstone, ``forgotten_at``),
        in one query, for any ids, read or not; an id it does not hold (purged, or never
        there) is not among them. A prune asks it of the rows it read with their tombstones
        (``--purge``), where a ``get_project`` of each asked again what was just read (review
        of #259, round 40, 6/6), and, without ``--purge``, of the candidates a read that
        leaves tombstones out did not find, to tell a forgotten row from a purged one (round
        41, 6/6; round 42, 11/11)."""
        named = sorted(set(project_ids))
        found: set[str] = set()
        for start in range(0, len(named), 500):  # well under SQLite's bound on parameters
            chunk = named[start : start + 500]
            rows = self._conn.execute(
                "SELECT id FROM project "
                f"WHERE forgotten_at IS NOT NULL AND id IN ({', '.join('?' * len(chunk))})",
                chunk,
            ).fetchall()
            found.update(str(row["id"]) for row in rows)
        return found

    def find_projects(self, term: str) -> list[ProjectInfo]:
        """The live projects ``term`` names: an id prefix, a directory name or a codename,
        and only when none of those match, a nickname (exactly, case and all).

        The nickname comes last so that one a person types never takes a reference
        that already worked: a nickname spelled like another project's codename or
        folder leaves that one found, rather than both refused as ambiguous.
        """
        rows = self._conn.execute(
            f"SELECT {_PROJECT_COLUMNS} FROM project "
            "WHERE forgotten_at IS NULL AND (id GLOB ? OR name = ? OR codename = ?) "
            "ORDER BY name",
            (_glob_prefix(term), term, term),
        ).fetchall()
        if not rows:
            rows = self._conn.execute(
                f"SELECT {_PROJECT_COLUMNS} FROM project "
                "WHERE forgotten_at IS NULL AND nickname = ? ORDER BY name",
                (term,),
            ).fetchall()
        return [_row_to_project(row) for row in rows]

    def forget_project(self, project_id: str) -> None:
        """Hide the registration; everything that references it stays put.

        A tombstone, not a delete — the FOREIGN KEYS from ``entry`` and
        ``prompt`` forbid deleting a project with history, and a plain forget
        should not cost history anyway. The context entries, prompt history,
        board rows, ended fleet-agent rows and turn metrics remain in the store,
        unreachable through any project read until the root is registered again.

        Its place in the arrangement does not stay (#140): the group, the
        position and the pin are cleared, and a project registered again comes
        back loose and unpinned, like a new one. Kept, the next prompt there
        revived a row that was still pinned and grouped, at a number its scope
        had renumbered away while it was forgotten — two rows on one slot,
        ordered by name (review of #171, round 1).
        """
        cursor = self._conn.execute(
            "UPDATE project SET forgotten_at = ?, onboarded_at = NULL, "
            "group_id = NULL, position = NULL, pinned_at = NULL "
            "WHERE id = ? AND forgotten_at IS NULL",
            (_now_iso(), project_id),
        )
        self._conn.commit()
        if cursor.rowcount != 1:
            raise KeyError(project_id)

    def purge_project(self, project_id: str) -> dict[str, int]:
        """Delete the registration AND every row that belongs to it, in one transaction.

        Dependents go first, so the ``project`` delete is legal under ``PRAGMA
        foreign_keys = ON``; the counts say what each table gave up. Every table
        whose foreign key references ``project`` is found in the schema
        (:meth:`_tables_referencing_project`), so a table a later migration adds
        is purged without being listed here. The team tables, ``brief``, ``fleet_agent``,
        ``metric`` and the token-usage tables carry no FK, so the schema cannot
        name them: they are
        listed, and deleted by ``project_id`` the way every read of them is
        keyed; the metric rows are written even with the CI test bed off, and
        left behind they resurfaced in ``metrics list --all`` and in the
        project's own scope if its root was registered again. ``team_meta`` is a
        key/value bag whose keys embed either the project id (the distiller's
        watermark, signals) or a session id (nudge debounce, continuation
        counters), so its rows are matched on both — in bounded statements, see
        :meth:`_purge_team_meta`. LIVE fleet agents are the caller's problem to
        refuse before getting here: this deletes their rows too, and a pane that
        is still running would then be unaccounted for.

        A key the CLI minted for the project (#142) is detached first, in the
        same transaction, and its revocation owed (:meth:`_owe_revocation`):
        the destination row is how anything here found that key, and a purge
        that deleted it took a live ``ingest:write`` key off every list this
        machine keeps (review of #172). The caller revokes it once the store
        session is closed.
        """
        sessions = [
            str(row["id"])
            for row in self._conn.execute(
                "SELECT id FROM team_session WHERE project_id = ?", (project_id,)
            ).fetchall()
        ]
        removed: dict[str, int] = {}
        with self._conn:  # one BEGIN…COMMIT: a purge is whole or it is nothing
            self._owe_revocation(project_id)
            for table, column in self._tables_referencing_project():
                cursor = self._conn.execute(
                    f'DELETE FROM "{table}" WHERE "{column}" = ?', (project_id,)
                )
                removed[table] = removed.get(table, 0) + cursor.rowcount
            for table in (
                "team_event",
                "team_task",
                "team_session",
                "fleet_agent",
                "metric",
                "usage_turn",
                "usage_session",
                "brief",
            ):
                if table in removed:
                    continue  # it has gained the FK, and went above
                cursor = self._conn.execute(
                    f"DELETE FROM {table} WHERE project_id = ?", (project_id,)
                )
                removed[table] = cursor.rowcount
            removed["team_meta"] = self._purge_team_meta(project_id, sessions)
            cursor = self._conn.execute("DELETE FROM project WHERE id = ?", (project_id,))
            removed["project"] = cursor.rowcount
        if removed["project"] != 1:
            raise KeyError(project_id)
        return removed

    def _tables_referencing_project(self) -> list[tuple[str, str]]:
        """Every ``(table, column)`` whose foreign key references ``project (id)``.

        Read from the schema rather than listed, because the list is what kept
        going wrong. Each branch that added a table with this FK listed its own
        here, and a merge between two of them kept one side's list: at the
        accounts stack's head the purge named ``project_destination`` but not
        ``project_explainability`` (listed on #170, lost above it) nor #205's
        ``project_setting``. A purge that met a row in a table it did not know
        failed on ``FOREIGN KEY constraint failed`` and rolled back whole, so
        ``project forget --purge`` and ``prune --purge`` could not remove
        exactly the projects those features had been used on (review of #172,
        and of the #205 fold).

        In schema order, so the counts come out the same way every time. A
        foreign key written ``REFERENCES project`` names no column and means the
        primary key, so it counts. ``project`` itself is left out: a row there
        that points at another project is that other project's, not this one's
        to delete.
        """
        rows = self._conn.execute(
            """
            SELECT m.name AS table_name, f."from" AS column_name
            FROM sqlite_master AS m JOIN pragma_foreign_key_list(m.name) AS f
            WHERE m.type = 'table' AND m.name != 'project'
              AND lower(f."table") = 'project' AND (f."to" IS NULL OR f."to" = 'id')
            ORDER BY m.rowid, f.id
            """
        ).fetchall()
        return [(str(row["table_name"]), str(row["column_name"])) for row in rows]

    def _purge_team_meta(self, project_id: str, sessions: list[str]) -> int:
        """Delete the ``team_meta`` rows keyed by the project or by its sessions.

        Inside the caller's transaction, in statements of at most
        :data:`_META_BATCH` sessions each (one term per :data:`_SESSION_META_KINDS` and
        one for its continuations): one statement per purge was the shape SQLite refused at
        500 sessions.
        """
        removed = self._conn.execute(
            "DELETE FROM team_meta WHERE key = ? OR key GLOB ? OR key GLOB ?",
            (
                f"distill_seq:{project_id}",
                f"signal/{_glob_escape(project_id)}/*",
                # the processes that run this board's sessions (``services.mcp_host``)
                f"host/{_glob_escape(project_id)}/*",
            ),
        ).rowcount
        for start in range(0, len(sessions), _META_BATCH):
            batch = sessions[start : start + _META_BATCH]
            keys = [f"{kind}:{sid}" for sid in batch for kind in _SESSION_META_KINDS]
            globs = [f"continuations:{_glob_escape(sid)}:*" for sid in batch]
            clauses = ["key = ?"] * len(keys) + ["key GLOB ?"] * len(globs)
            removed += self._conn.execute(
                f"DELETE FROM team_meta WHERE {' OR '.join(clauses)}", (*keys, *globs)
            ).rowcount
        return removed

    def project_activity(self) -> dict[str, str]:
        """When each visible project was last touched, as an ISO-8601 UTC string.

        The project row itself records only ``created_at``; "touched" is the
        newest of that and every timestamp the project's rows carry — a fact
        updated, a prompt captured, a session seen, a task moved, an event
        posted, an agent spawned. Uniform ISO strings compare lexicographically,
        which is what lets one ``MAX`` span six tables. Used to pick where the
        active pin lands when the active project is forgotten.

        A table the store lacks (doctor's damaged-store case) has nothing to add: named,
        "no such table" ended every captured-row prune, and doctor's capture-off line was
        dropped without a word (review of #259, round 40, 1/6).
        """
        touched = "".join(
            f" UNION ALL {select}" for table, select in _ACTIVITY.items() if table in self._names
        )
        rows = self._conn.execute(
            "SELECT p.id, MAX(t.at) AS at FROM project p JOIN ("
            f"  SELECT id AS project_id, created_at AS at FROM project{touched}"
            ") t ON t.project_id = p.id "
            "WHERE p.forgotten_at IS NULL GROUP BY p.id"
        ).fetchall()
        return {str(row["id"]): str(row["at"]) for row in rows}

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
        """The most recent prompts, newest first, INSERTION ORDER within a tick.

        The tie-break is ``rowid`` rather than ``id``. An id is a millisecond
        stamp followed by 128 random bits, so two prompts written inside one
        tick used to sort by coin flip — and `aisquare board` run twice could
        show the same two in different orders. Windows showed it most, because
        its clock is coarser: `time.time()` only gained
        ``GetSystemTimePreciseAsFileTime`` in 3.13 and the CI lane pins 3.12, so
        back-to-back inserts routinely land in the same millisecond.

        ``rowid`` is monotonic per insert and costs nothing: ``prompt`` is a
        plain table (``id TEXT PRIMARY KEY``, no ``WITHOUT ROWID``), so SQLite
        already maintains one. The suite used to sleep past this; ordering by
        the column that means "written after" removes the wart instead.
        """
        # A forgotten project's prompts are hidden with the rest of its history.
        if project_id is None:
            rows = self._conn.execute(
                f"SELECT {_PROMPT_COLUMNS} FROM prompt "
                f"WHERE project_id IS NULL OR project_id IN {_VISIBLE_PROJECTS} "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"SELECT {_PROMPT_COLUMNS} FROM prompt "
                f"WHERE project_id = ? AND project_id IN {_VISIBLE_PROJECTS} "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
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
        """Insert the session, or revive/refresh it if the id is already known.

        A known session keeps the board it first registered on, and its cursor, as on
        main: the captain line's move to another board (#230's fix 2b) repaired the
        wrong-board sessions of #230's own hub rule, which hub rule A retired, and
        shipped a change to main's semantics unpinned (review of #240, F4).
        """
        self._conn.execute(
            f"INSERT INTO team_session ({_SESSION_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET "
            "last_seen_at = excluded.last_seen_at, ended_at = NULL, "
            "state = 'working', "
            "transcript_path = COALESCE(excluded.transcript_path, transcript_path), "
            "account = COALESCE(excluded.account, account), "
            "model = COALESCE(excluded.model, model), "
            "effort = COALESCE(excluded.effort, effort), "
            "persona = COALESCE(excluded.persona, persona), "
            "prompted_at = COALESCE(excluded.prompted_at, prompted_at)",
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
                session.limit_resets_at.isoformat() if session.limit_resets_at else None,
                session.persona,
                session.prompted_at.isoformat() if session.prompted_at else None,
            ),
        )
        self._conn.commit()
        stored = self.get_session(session.id)
        assert stored is not None  # just upserted
        return stored

    def get_session(
        self, session_id: str, *, project_ids: Collection[str] | None = None
    ) -> TeamSession | None:
        """The session ``session_id`` names, an id or an id prefix; with ``project_ids``, among
        the sessions of those boards only, ambiguity included (:func:`_on_boards`)."""
        scope, boards = _on_boards(project_ids)
        row = self._conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM team_session WHERE id = ?{scope}",
            (session_id, *boards),
        ).fetchone()
        if row is not None:
            return _row_to_session(row)
        matches = self._conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM team_session WHERE id GLOB ?{scope} LIMIT 2",
            (_glob_prefix(session_id), *boards),
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

    def team_sessions_seen_since(
        self, project_id: str, since: datetime | None, *, ids: Sequence[str] = ()
    ) -> list[TeamSession]:
        """The project's sessions seen at or after ``since``, and those named in ``ids`` however
        long ago they were seen; newest seen first. ``None`` for ``since``: those named alone.

        For a reader that runs every few seconds and needs the live few (``remote``'s needs
        scan), or the few its events name (``remote``'s board frame): :meth:`team_sessions`
        is every session the project ever had, and every Claude Code start adds one that is
        never deleted. The window rides the ``(project_id, last_seen_at)`` index; ``since``
        compares as the stored ISO-8601 UTC strings do.
        """
        rows: list[sqlite3.Row] = []
        if since is not None:
            rows = self._conn.execute(
                f"SELECT {_SESSION_COLUMNS} FROM team_session "
                "WHERE project_id = ? AND last_seen_at >= ?",
                (project_id, since.astimezone(UTC).isoformat()),
            ).fetchall()
        named = sorted(set(ids))
        for start in range(0, len(named), 500):  # well under SQLite's bound on parameters
            chunk = named[start : start + 500]
            rows += self._conn.execute(
                f"SELECT {_SESSION_COLUMNS} FROM team_session "
                f"WHERE project_id = ? AND id IN ({', '.join('?' * len(chunk))})",
                (project_id, *chunk),
            ).fetchall()
        sessions = {row["id"]: _row_to_session(row) for row in rows}
        return sorted(sessions.values(), key=lambda session: session.last_seen_at, reverse=True)

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

        ``state`` never replaces a hand-over's mark (``'switching'``,
        ``services.team.HANDOVER_STATE``), and neither do :meth:`mark_attention`
        and :meth:`mark_limited`. ``fleet switch`` sets it before it ``/exit``s
        the agent, and the session's own ``SessionEnd`` reads it to park the
        claims for the replacement instead of releasing them. Each hook that
        wrote a state in between replaced it — the ``Stop`` of a turn ending
        within ``stop``'s grace, a permission prompt's ``attention`` — and that
        ``SessionEnd`` released the claims the replacement was to inherit (review
        of the #205 fold, round 1). Kept here, so a state writer added later
        cannot forget it the way those hooks did. Only the session's own start
        (:meth:`upsert_session`, the resumed agent) and ``switch`` or ``restart``
        taking its mark back (:meth:`unmark_handover`) replace it. The rest of
        the heartbeat still lands.
        """
        sets, params = ["last_seen_at = ?", "ended_at = NULL"], [_now_iso()]
        if cursor is not None:
            sets.append("cursor = ?")
            params.append(str(cursor))
        if state is not None:
            sets.append("state = CASE WHEN state = 'switching' THEN state ELSE ? END")
            params.append(state)
        self._conn.execute(
            f"UPDATE team_session SET {', '.join(sets)} WHERE id = ?",
            (*params, session_id),
        )
        self._conn.commit()

    def replace_session_state(self, session_id: str, expected: str, state: str) -> bool:
        """Set ``state`` on a session whose state is still ``expected`` — and nothing else.

        A compare-and-set for a writer that takes a state BACK: a hand-over that
        did not happen returning its mark (``team.HANDOVER_STATE``) to what the
        session said before. :meth:`touch_session` is a heartbeat — it bumps
        ``last_seen_at`` and clears ``ended_at`` — and taking a mark back is no
        evidence of life: a session whose ``SessionEnd`` had already run came back
        to the board as a live teammate (review of #163, round 2). Nor is it
        newer than a state written since the mark, which it leaves alone. Returns
        whether the row changed.
        """
        cursor = self._conn.execute(
            "UPDATE team_session SET state = ? WHERE id = ? AND state = ?",
            (state, session_id, expected),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def mark_handover(self, session_id: str, *, held_after: datetime) -> bool:
        """Set a hand-over's mark (``'switching'``) where no other hand-over holds one.

        A compare-and-set. ``fleet switch`` and ``restart`` of a running agent read
        the session, refused one a hand-over held, and then marked it with
        :meth:`touch_session`, which writes unconditionally. Two of them on one
        agent (two restarts, a restart and a switch, a switch by hand and the
        automatic one) both got past that read and both marked it, and both
        started a replacement (review of #203). The mark is held when the state
        is ``'switching'`` and was written after ``held_after``, which is the
        caller's rule (``fleet._handed_over``: after the row being moved was
        created, and within the grace). Then nothing is written and this returns
        False. Otherwise it is the heartbeat :meth:`touch_session` is, with the
        mark as its state, and returns True. A session row that is not there
        returns False too, as there is nothing to mark.
        """
        cursor = self._conn.execute(
            "UPDATE team_session SET state = 'switching', last_seen_at = ?, ended_at = NULL "
            "WHERE id = ? AND NOT (state = 'switching' AND last_seen_at > ?)",
            (_now_iso(), session_id, held_after.astimezone(UTC).isoformat()),
        )
        self._conn.commit()
        return cursor.rowcount == 1

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

        Never over a hand-over's mark (:meth:`touch_session`): no transition, so
        no bell either, for an agent ``fleet switch`` is exiting.
        """
        cursor = self._conn.execute(
            "UPDATE team_session SET state = 'attention', last_seen_at = ? "
            "WHERE id = ? AND state NOT IN ('attention', 'switching')",
            (_now_iso(), session_id),
        )
        self._conn.execute(
            "UPDATE team_session SET last_seen_at = ?, ended_at = NULL WHERE id = ?",
            (_now_iso(), session_id),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def set_transcript_path(self, session_id: str, transcript_path: str) -> None:
        """Point the row at the transcript its agent writes, and touch nothing else (P0-W3b).

        For a row whose recorded transcript is not on disk: a cross-account resume reports
        one under the new account that Claude Code never creates. ``upsert_session`` would
        also revive the row and set it ``working``, over a hand-over's or a limit's state.
        """
        self._conn.execute(
            "UPDATE team_session SET transcript_path = ? WHERE id = ?",
            (transcript_path, session_id),
        )
        self._conn.commit()

    def mark_limited(self, session_id: str, resets_at: datetime | None) -> None:
        """The session's turn ended on a usage limit (#146): park it as ``limited``.

        Unconditional, unlike :meth:`mark_attention`: a second limit in the same
        window carries a newer reset time, and the feed de-duplication that
        ``attention`` needs is done by the caller, which knows the previous state.
        Un-retires the row for the same reason ``mark_attention`` does — a
        limited agent is alive and is exactly the one an operator is looking for.
        Never over a hand-over's mark (:meth:`touch_session`).
        """
        self._conn.execute(
            "UPDATE team_session SET "
            "state = CASE WHEN state = 'switching' THEN state ELSE 'limited' END, "
            "limit_resets_at = ?, last_seen_at = ?, ended_at = NULL WHERE id = ?",
            (resets_at.isoformat() if resets_at is not None else None, _now_iso(), session_id),
        )
        self._conn.commit()

    def unmark_handover(self, session_id: str, state: str) -> None:
        """``fleet switch`` or ``restart`` takes its hand-over mark back: its stop has ended.

        The one writer besides the session's own start that replaces the mark
        (:meth:`touch_session` keeps it), and it replaces only the mark: a row
        that has moved on since is not written back to ``state``.
        """
        self._conn.execute(
            "UPDATE team_session SET state = ? WHERE id = ? AND state = 'switching'",
            (state, session_id),
        )
        self._conn.commit()

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

        All or nothing: a failure rolls the transaction back before it raises.
        A COMMIT the database refuses (``database is locked``) leaves the
        transaction open with the UPDATE in it, and the connection's next
        commit made it land anyway. ``fleet switch`` reported "was not marked
        ended" for a session its own claim move then ended, and a caller whose
        next write rolled back discarded it without a word (review of the #205
        fold, A1).
        """
        now = _now_iso()
        try:
            released = self._doing_claims(session_id)
            if release_claims:
                self._release_doing_claims(session_id, now)
            self._conn.execute(
                "UPDATE team_session SET ended_at = ?, last_seen_at = ? WHERE id = ?",
                (now, now, session_id),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return released

    def release_claims(self, session_id: str) -> list[TeamTask]:
        """Return the session's ``doing`` claims to the pool — its presence row untouched.

        The other half of :meth:`end_session`, on its own: for a session that
        already ENDED with its claims kept (a fleet agent's ``/clear`` parks
        them for the start hook that follows) whose fleet row then ended before
        anything came back for them. Ending the session again would only move
        its ``ended_at``; the claims are what is owed (review of #135, second
        round, finding 5).
        """
        released = self._doing_claims(session_id)
        self._release_doing_claims(session_id, _now_iso())
        self._conn.commit()
        return released

    def _doing_claims(self, session_id: str) -> list[TeamTask]:
        return [
            _row_to_task(row)
            for row in self._conn.execute(
                f"SELECT {_TASK_COLUMNS} FROM team_task WHERE claimed_by = ? AND status = 'doing'",
                (session_id,),
            ).fetchall()
        ]

    def _release_doing_claims(self, session_id: str, now: str) -> None:
        self._conn.execute(
            "UPDATE team_task SET status = 'todo', claimed_by = NULL, "
            "claim_expires_at = NULL, updated_at = ? WHERE claimed_by = ? AND status = 'doing'",
            (now, session_id),
        )

    def upsert_task(self, task: TeamTask) -> tuple[TeamTask, bool]:
        """Add a task; a duplicate ``(project_id, key)`` returns the existing one.

        The second element reports whether a new task was created — the
        idempotency contract for ``task add``.

        The task's number is the board's highest plus one, read in the INSERT itself,
        so it is taken under the write lock with the row and two adds cannot share one.
        A duplicate key inserts nothing and so takes no number. ``task.number`` is
        ignored: the store gives it.
        """
        cursor = self._conn.execute(
            f"INSERT INTO team_task ({_TASK_COLUMNS}) "
            "VALUES (?, ?, (SELECT COALESCE(MAX(number), 0) + 1 FROM team_task "
            "WHERE project_id = ?), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (project_id, key) DO NOTHING",
            (
                task.id,
                task.project_id,
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

    def get_task(self, ref: str, *, project_ids: Collection[str] | None = None) -> TeamTask | None:
        """The task ``ref`` names, an id, an id prefix or an id tail; with ``project_ids``,
        among the tasks of those boards only, ambiguity included (:func:`_on_boards`)."""
        scope, boards = _on_boards(project_ids)
        row = self._conn.execute(
            f"SELECT {_TASK_COLUMNS} FROM team_task WHERE id = ?{scope}", (ref, *boards)
        ).fetchone()
        if row is not None:
            return _row_to_task(row)
        matches = self._conn.execute(
            f"SELECT {_TASK_COLUMNS} FROM team_task WHERE id GLOB ?{scope} LIMIT 2",
            (_glob_prefix(ref), *boards),
        ).fetchall()
        if len(matches) > 1:
            raise AmbiguousIdError(ref)
        if matches:
            return _row_to_task(matches[0])
        if not _ref_stripped(ref):
            return None  # no tail either: ``*`` alone ends every id
        # Boards display the id *tail* (the unique random part), so tails
        # must resolve too: fall back to a suffix match.
        matches = self._conn.execute(
            f"SELECT {_TASK_COLUMNS} FROM team_task WHERE id GLOB ?{scope} LIMIT 2",
            (f"*{_ref_stripped(ref)}", *boards),
        ).fetchall()
        if len(matches) > 1:
            raise AmbiguousIdError(ref)
        return _row_to_task(matches[0]) if matches else None

    def task_by_number(self, project_id: str, number: int) -> TeamTask | None:
        """The task numbered ``number`` on ``project_id``'s board (``#12``), if there is one."""
        row = self._conn.execute(
            f"SELECT {_TASK_COLUMNS} FROM team_task WHERE project_id = ? AND number = ?",
            (project_id, number),
        ).fetchone()
        return _row_to_task(row) if row is not None else None

    def task_numbers(self, project_id: str) -> dict[str, int]:
        """Every numbered task of the board, id → number: for lines that name a task by id."""
        rows = self._conn.execute(
            "SELECT id, number FROM team_task WHERE project_id = ? AND number IS NOT NULL",
            (project_id,),
        ).fetchall()
        return {row["id"]: row["number"] for row in rows}

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

    def has_team_task_in(self, project_id: str, statuses: Collection[TaskStatus]) -> bool:
        """Whether the board holds a task in any of ``statuses``: one ``EXISTS`` on the
        ``(project_id, status)`` index, never the board's every task read and decoded."""
        wanted = sorted(statuses)
        if not wanted:
            return False
        marks = ", ".join("?" for _ in wanted)
        row = self._conn.execute(
            f"SELECT EXISTS (SELECT 1 FROM team_task WHERE project_id = ? AND status IN ({marks}))",
            (project_id, *wanted),
        ).fetchone()
        return bool(row[0])

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
        if status in CLOSED_STATUSES:
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
        self,
        project_id: str,
        *,
        role: str | None = None,
        status: TaskStatus = "todo",
        prefer: str | None = None,
        only: str | None = None,
    ) -> TeamTask | None:
        """The oldest *ready* task in ``status`` a session of ``role`` could pick up.

        ``only`` narrows the pool to that one task id, under the same rules (``task next
        --task``: a tester spawned for one task takes it and nothing else, #129 a).

        Tasks without a role hint are available to every role; tasks with one
        only match sessions of that role (or an unfiltered query). A ``todo``
        task is ready only when every task it needs is resolved — so loopers
        never receive work whose prerequisites are still in flight.

        ``prefer`` puts one task first in the order — the one a fleet agent was
        spawned for — under exactly the same status, role and readiness rules
        as every other candidate: one predicate, one query, not a copy of it.
        """
        clauses = ["project_id = ?", "status = ?"]
        params: list[str] = [project_id, status]
        if role is not None:
            clauses.append("(role IS NULL OR role = ?)")
            params.append(role)
        if only is not None:
            clauses.append("id = ?")
            params.append(only)
        order = "ORDER BY id"
        if prefer is not None:
            order = "ORDER BY (id = ?) DESC, id"
            params.append(prefer)
        rows = self._conn.execute(
            f"SELECT {_TASK_COLUMNS} FROM team_task WHERE {' AND '.join(clauses)} {order}",
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

    def open_turn(self, metric: TurnMetric) -> TurnMetric:
        """Record the start of a turn. One INSERT — this is the hot path.

        A plain INSERT, not ``INSERT OR REPLACE``: the trace id is freshly
        minted, so a collision is a bug, and a bug should raise rather than
        silently destroy the row it collided with.
        """

        def flag(value: bool | None) -> int | None:
            return None if value is None else int(value)

        values = (
            metric.trace_id,
            metric.project_id,
            metric.session_id,
            metric.started_at.isoformat(),
            metric.ended_at.isoformat() if metric.ended_at else None,
            metric.wall_ms,
            metric.run_id,
            metric.run_kind,
            metric.opaque_config_id,
            metric.trigger,
            metric.client_reason.value,
            metric.status,
            metric.action,
            metric.query_id,
            metric.briefing_id,
            metric.config_fingerprint,
            metric.input_checkpoint,
            metric.resolved_scope_version,
            metric.round_trip_ms,
            metric.server_ms,
            flag(metric.deadline_breached),
            metric.token_count,
            metric.items_count,
            metric.cache_status,
            json.dumps(list(metric.error_codes)),
            metric.rendered_chars,
            metric.injected_chars,
            metric.frame_version,
            metric.instruction_version,
            metric.redaction_level.value if metric.redaction_level else None,
            metric.snapshot_ref,
            flag(metric.snapshot_untracked_excluded),
            metric.tokens_in,
            metric.tokens_out,
            metric.tool_calls,
            metric.delivery_source,
        )
        placeholders = ", ".join("?" for _ in _METRIC_FIELDS)
        self._conn.execute(
            f"INSERT INTO metric ({_METRIC_COLUMNS}) VALUES ({placeholders})", values
        )
        self._conn.commit()
        return metric

    def close_turn(self, session_id: str, *, ended_at: datetime) -> TurnMetric | None:
        """Close this session's newest open turn; ``None`` when nothing was closed.

        Matched on the session rather than carried through the agent, because
        the Stop hook payload has no trace id in it — only the session. The
        newest open row is the turn that just finished. A turn that never
        received a Stop (a killed terminal) stays open and is excluded from
        wall-clock aggregates rather than closed with a fabricated end time;
        an open row older than :data:`MAX_OPEN_TURN` is left alone for the same
        reason, so an orphan cannot absorb a three-day gap into the median.

        The UPDATE is a compare-and-set on ``ended_at IS NULL``: two Stops that
        both saw the row open cannot both write it, and the loser reports
        ``None`` rather than overwriting the first close. A negative clock
        delta is stored as ``NULL``, not clamped to a zero that would read as
        a real instant turn.
        """
        row = self._conn.execute(
            "SELECT trace_id, started_at FROM metric "
            "WHERE session_id = ? AND ended_at IS NULL "
            "ORDER BY started_at DESC, trace_id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        started_at = datetime.fromisoformat(row["started_at"])
        delta = ended_at - started_at
        if delta > MAX_OPEN_TURN:
            return None
        wall_ms = int(delta.total_seconds() * 1000) if delta >= timedelta(0) else None
        cursor = self._conn.execute(
            "UPDATE metric SET ended_at = ?, wall_ms = ? WHERE trace_id = ? AND ended_at IS NULL",
            (ended_at.isoformat(), wall_ms, row["trace_id"]),
        )
        self._conn.commit()
        if cursor.rowcount == 0:
            return None
        closed = self._conn.execute(
            f"SELECT {_METRIC_COLUMNS} FROM metric WHERE trace_id = ?", (row["trace_id"],)
        ).fetchone()
        return _row_to_metric(closed) if closed is not None else None

    def turn_metrics(
        self, *, project_id: str | None = None, session_id: str | None = None, limit: int = 500
    ) -> list[TurnMetric]:
        """Recorded turns, newest first. ``limit`` is clamped to at least one:
        SQLite reads a negative LIMIT as unbounded, and the whole table is not
        what anyone asked for."""
        clauses: list[str] = []
        params: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        params.append(max(1, int(limit)))
        rows = self._conn.execute(
            f"SELECT {_METRIC_COLUMNS} FROM metric {where}"
            "ORDER BY started_at DESC, trace_id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [_row_to_metric(row) for row in rows]

    # --- session token usage, read off the transcript by the hooks ---------------------

    def session_usage(self, session_id: str) -> SessionUsage | None:
        """The session's usage row, with the scan state the next Stop resumes from."""
        row = self._conn.execute(
            f"SELECT {_SESSION_USAGE_COLUMNS} FROM usage_session WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return _row_to_session_usage(row) if row is not None else None

    def record_usage(
        self,
        session: SessionUsage,
        turn: TurnUsage | None,
        *,
        previous_state: str | None,
        metric_counts: tuple[int, int, int] | None = None,
    ) -> bool:
        """Write a scan's result as ONE transaction; ``False`` when another writer won.

        The session row is a compare-and-set on the state the scan started from
        (``previous_state``; ``None`` for a session with no row yet): two hooks
        that scanned the same bytes — a Stop and a SessionEnd racing — cannot
        both count them, and the loser writes nothing at all, its turn row
        included. ``metric_counts`` (tokens in, tokens out, tool calls) fill the
        CI test bed's columns on the ``metric`` row the turn closed: the
        transcript is the evidence those columns were waiting for.
        """
        values = (
            session.project_id,
            session.transcript_path,
            session.state,
            json.dumps(session.usage, separators=(",", ":")),
            session.turns,
            session.total_tokens,
            session.weighted,
            session.cost_usd,
            session.updated_at.isoformat(),
            session.ended_at.isoformat() if session.ended_at else None,
        )
        try:
            if previous_state is None:
                cursor = self._conn.execute(
                    "INSERT INTO usage_session (project_id, transcript_path, state, usage, turns, "
                    "total_tokens, weighted, cost_usd, updated_at, ended_at, session_id, "
                    "started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (session_id) DO NOTHING",
                    (*values, session.session_id, session.started_at.isoformat()),
                )
            else:
                cursor = self._conn.execute(
                    "UPDATE usage_session SET project_id = ?, transcript_path = ?, state = ?, "
                    "usage = ?, turns = ?, total_tokens = ?, weighted = ?, cost_usd = ?, "
                    "updated_at = ?, ended_at = ? WHERE session_id = ? AND state = ?",
                    (*values, session.session_id, previous_state),
                )
            if cursor.rowcount == 0:
                self._conn.rollback()
                return False
            if turn is not None:
                self._conn.execute(
                    f"INSERT INTO usage_turn ({_TURN_USAGE_COLUMNS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        turn.id,
                        turn.session_id,
                        turn.project_id,
                        turn.trace_id,
                        turn.seq,
                        turn.trigger,
                        turn.recorded_at.isoformat(),
                        json.dumps(turn.usage, separators=(",", ":")),
                        turn.total_tokens,
                        turn.weighted,
                        turn.cost_usd,
                        turn.scan_ms,
                    ),
                )
                if turn.trace_id is not None and metric_counts is not None:
                    self._conn.execute(
                        "UPDATE metric SET tokens_in = ?, tokens_out = ?, tool_calls = ? "
                        "WHERE trace_id = ?",
                        (*metric_counts, turn.trace_id),
                    )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return True

    def session_usages(
        self,
        *,
        project_id: str | None = None,
        session_id: str | None = None,
        limit: int = 10_000,
    ) -> list[SessionUsage]:
        """Sessions' usage rows, most recently updated first. ``session_id`` matches
        a prefix, as the board's short ids are typed."""
        clauses: list[str] = []
        params: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if session_id is not None:
            clauses.append("session_id LIKE ? ESCAPE '\\'")
            params.append(_like_prefix(session_id))
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        params.append(max(1, int(limit)))
        rows = self._conn.execute(
            f"SELECT {_SESSION_USAGE_COLUMNS} FROM usage_session {where}"
            "ORDER BY updated_at DESC, session_id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [_row_to_session_usage(row) for row in rows]

    def turn_usages(self, session_id: str) -> list[TurnUsage]:
        """One session's turn rows, in order."""
        rows = self._conn.execute(
            f"SELECT {_TURN_USAGE_COLUMNS} FROM usage_turn WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
        return [_row_to_turn_usage(row) for row in rows]

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

    def delete_meta(self, key: str) -> None:
        self._conn.execute("DELETE FROM team_meta WHERE key = ?", (key,))
        self._conn.commit()

    def list_meta(self, prefix: str) -> dict[str, str]:
        """Every ``team_meta`` entry under ``prefix``, key → value."""
        rows = self._conn.execute(
            "SELECT key, value FROM team_meta WHERE key GLOB ? ORDER BY key",
            (f"{_glob_escape(prefix)}*",),
        ).fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def add_signal_events(
        self, writes: Sequence[tuple[TeamEvent, str, dict[str, Any]]]
    ) -> list[TeamEvent]:
        """Insert each signal event of ``writes`` AND its meta materialization, all in ONE
        transaction.

        A signal is two writes — the pipe event (history) and the current-state
        blob under its meta key — and a crash between separate commits would
        leave watchers and readers disagreeing. The event's assigned ``seq``
        is injected into its meta value before the blob is stored, so the
        state row always names the event that produced it. Every signal of
        ``writes`` is set, or a raise rolls all of them back: a project's pause is
        one signal on each board it lives on, and set one commit a board, a store
        error part-way left it paused on some boards and not on others (review of
        #259, round 34, 5/10).
        """
        written: list[TeamEvent] = []
        with self._conn:  # one BEGIN…COMMIT for every statement
            for event, meta_key, meta_value in writes:
                cursor = self._conn.execute(
                    "INSERT INTO team_event (id, project_id, session_id, kind, text, "
                    "task_id, to_role, created_at, actor, meta) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _event_values(event),
                )
                stamped = {**meta_value, "seq": cursor.lastrowid}
                self._conn.execute(
                    "INSERT INTO team_meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (meta_key, json.dumps(stamped)),
                )
                written.append(event.model_copy(update={"seq": cursor.lastrowid}))
        return written

    def _insert_event(self, event: TeamEvent) -> TeamEvent:
        """One ``team_event`` row in the caller's transaction, returned with its ``seq``."""
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
        return event.model_copy(update={"seq": cursor.lastrowid})

    def add_brief(
        self, brief_id: str, project_id: str, data: str, created_at: datetime, event: TeamEvent
    ) -> tuple[int, TeamEvent]:
        """Insert a new brief at revision 1 with its board event, in one transaction; its
        number on the board and the stored event, whose text is prefixed with the brief's
        label (``B3 r1: ``), known only once the number is.

        The number is the board's highest plus one, read in the INSERT itself, as
        :meth:`upsert_task` numbers tasks, so two creates cannot share one.
        """
        with self._conn:
            self._conn.execute(
                "INSERT INTO brief (id, project_id, number, revision, data, created_at, "
                "updated_at) SELECT ?, ?, COALESCE(MAX(number), 0) + 1, 1, ?, ?, ? "
                "FROM brief WHERE project_id = ?",
                (brief_id, project_id, data, created_at.isoformat(), created_at.isoformat(),
                 project_id),
            )  # fmt: skip
            row = self._conn.execute("SELECT number FROM brief WHERE id = ?", (brief_id,))
            number = int(row.fetchone()[0])
            stored = self._insert_event(
                event.model_copy(update={"text": f"B{number} r1: {event.text}"})
            )
        return number, stored

    def get_brief(self, project_id: str, ref: str) -> BriefRow | None:
        """The brief ``ref`` names on ``project_id``'s board: its number (``3``, ``#3``,
        ``B3``), else its id, an id prefix or an id tail. Never another board's."""
        number = _BRIEF_NUMBER_REF.match(ref.strip())
        if number is not None:
            row = self._conn.execute(
                f"SELECT {_BRIEF_COLUMNS} FROM brief WHERE project_id = ? AND number = ?",
                (project_id, int(number.group(1))),
            ).fetchone()
            return _row_to_brief(row) if row is not None else None
        row = self._conn.execute(
            f"SELECT {_BRIEF_COLUMNS} FROM brief WHERE project_id = ? AND id = ?",
            (project_id, ref),
        ).fetchone()
        if row is not None:
            return _row_to_brief(row)
        stripped = _ref_stripped(ref)
        if not stripped:
            return None
        for pattern in (f"{stripped}*", f"*{stripped}"):
            matches = self._conn.execute(
                f"SELECT {_BRIEF_COLUMNS} FROM brief WHERE project_id = ? AND id GLOB ? LIMIT 2",
                (project_id, pattern),
            ).fetchall()
            if len(matches) > 1:
                raise AmbiguousIdError(ref)
            if matches:
                return _row_to_brief(matches[0])
        return None

    def briefs(self, project_id: str) -> list[BriefRow]:
        """Every brief on ``project_id``'s board, by number."""
        rows = self._conn.execute(
            f"SELECT {_BRIEF_COLUMNS} FROM brief WHERE project_id = ? ORDER BY number",
            (project_id,),
        ).fetchall()
        return [_row_to_brief(row) for row in rows]

    def all_brief_documents(self) -> list[str]:
        """Every brief's stored document on every board, a forgotten project's included:
        report retention keeps what their evidence cites, and a forgotten project comes
        back with its briefs when its root registers again."""
        return [str(row[0]) for row in self._conn.execute("SELECT data FROM brief").fetchall()]

    def complete_task_if_unchanged(self, task_id: str, briefs: Mapping[str, int]) -> TeamTask:
        """Mark a task done, in one ``BEGIN IMMEDIATE`` transaction with the check that every
        brief linking it is still at the revision its evidence gate read (``briefs``).

        A brief linked to the task that changed since, or that links it now and was not
        read, raises :class:`BriefChangedError` and writes nothing: a correction landing
        between the gate and the write fails the completion. A brief that does not name
        the task is never read, and one that names it but cannot be parsed counts as
        linked. The write itself is :meth:`set_task_status`'s for ``done``.
        """
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            task = self.get_task(task_id)
            if task is None:
                raise KeyError(task_id)
            rows = self._conn.execute(
                "SELECT id, revision, data FROM brief WHERE project_id = ?", (task.project_id,)
            ).fetchall()
            for row in rows:
                data = str(row["data"])
                if task.id not in data:
                    continue
                try:
                    document = json.loads(data)
                    linked = any(
                        task.id in requirement.get("task_ids", [])
                        for requirement in document.get("requirements", [])
                    )
                except (ValueError, AttributeError, TypeError):
                    linked = True
                if linked and briefs.get(str(row["id"])) != int(row["revision"]):
                    raise BriefChangedError(str(row["id"]))
            self._conn.execute(
                "UPDATE team_task SET status = 'done', claimed_by = NULL, "
                "claim_expires_at = NULL, updated_at = ? WHERE id = ?",
                (_now_iso(), task.id),
            )
        updated = self.get_task(task.id)
        assert updated is not None  # just updated
        return updated

    def save_brief(
        self,
        brief_id: str,
        project_id: str,
        *,
        expected: int,
        revision: int,
        data: str,
        updated_at: datetime,
        event: TeamEvent,
        transitions: Sequence[TaskTransition] = (),
    ) -> list[TeamEvent]:
        """Replace a brief's document, move the tasks it sends back, and write the events,
        in one transaction; the stored events, the brief's first.

        Compare-and-swap on ``revision``: a writer that read ``expected`` and finds
        another revision raises :class:`BriefChangedError` and changes nothing, so two
        writers never overwrite each other's evidence. Each transition is guarded by
        the task's status in its own UPDATE: ``reopen`` takes a task in review or done
        back to todo and unowned; ``block`` blocks a task that is neither dropped nor
        blocked, and keeps its owner, the one who has to see why. A task whose status
        moved on since the caller read it is left as it is, and gets no event. Each
        moved task's event carries ``event``'s session, so the session whose write
        moved it is not woken by it.
        """
        stored: list[TeamEvent] = []
        with self._conn:
            changed = self._conn.execute(
                "UPDATE brief SET revision = ?, data = ?, updated_at = ? "
                "WHERE id = ? AND project_id = ? AND revision = ?",
                (revision, data, updated_at.isoformat(), brief_id, project_id, expected),
            ).rowcount
            if changed != 1:
                raise BriefChangedError(brief_id)
            stored.append(self._insert_event(event))
            for task_id, action, text in transitions:
                if action == "reopen":
                    moved = self._conn.execute(
                        "UPDATE team_task SET status = 'todo', claimed_by = NULL, "
                        "claim_expires_at = NULL, updated_at = ? "
                        "WHERE id = ? AND project_id = ? AND status IN ('review', 'done')",
                        (_now_iso(), task_id, project_id),
                    ).rowcount
                    kind = "task_reopened"
                else:
                    moved = self._conn.execute(
                        "UPDATE team_task SET status = 'blocked', updated_at = ? "
                        "WHERE id = ? AND project_id = ? AND status NOT IN ('dropped', 'blocked')",
                        (_now_iso(), task_id, project_id),
                    ).rowcount
                    kind = "task_blocked"
                if moved == 1:
                    stored.append(
                        self._insert_event(
                            event.model_copy(
                                update={
                                    "id": f"{event.id}-{task_id}",
                                    "kind": kind,
                                    "text": text,
                                    "task_id": task_id,
                                    "to_role": None,
                                }
                            )
                        )
                    )
        return stored

    def add_team_event(self, event: TeamEvent) -> TeamEvent:
        cursor = self._conn.execute(
            "INSERT INTO team_event (id, project_id, session_id, kind, text, "
            "task_id, to_role, created_at, actor, meta) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _event_values(event),
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
        exclude_kinds: Collection[str] = (),
        limit: int = 30,
    ) -> list[TeamEvent]:
        """Matching events for ``team log``'s filters, oldest-first.

        Without ``since_seq`` this is a window: the NEWEST ``limit`` matches.
        With ``since_seq`` it is a cursor page: the OLDEST ``limit`` matches
        past that seq, so a client tracking the last seq it saw never skips
        events (the MCP ``team_log`` contract). Rides the ``(project_id,
        seq)`` index; ``since_iso`` compares stored ISO-8601 UTC strings
        lexicographically (uniform format by construction). ``session_id`` is
        exact — prefix resolution is the service layer's job. ``exclude_kinds``
        leaves those kinds out INSIDE the query, so a window stays ``limit``
        long however many of them the board holds (the captain's audit lines).
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
        if exclude_kinds:
            # In the query, not after it: a window of N filtered AFTER the LIMIT
            # comes back short on a board where the excluded kind is common.
            clauses.append(f"kind NOT IN ({', '.join('?' * len(exclude_kinds))})")
            params.extend(exclude_kinds)
        paging = since_seq is not None
        rows = self._conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM team_event "
            f"WHERE {' AND '.join(clauses)} ORDER BY seq {'ASC' if paging else 'DESC'} LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [_row_to_event(row) for row in (rows if paging else reversed(rows))]

    def events_since(
        self,
        project_id: str,
        seq: int,
        *,
        exclude_session: str | None = None,
        exclude_kinds: Collection[str] = (),
        exclude_actor: str | None = None,
        relevant: Relevance | None = None,
        limit: int = 50,
    ) -> list[TeamEvent]:
        """A cursor page: the OLDEST ``limit`` events past ``seq``, oldest first.

        ``exclude_actor`` leaves out what that session caused, as ``exclude_session`` leaves
        out what it wrote: a manager's own ``fleet stop`` is written under the stopped
        agent's session, with the manager as its actor. ``relevant`` keeps only what a
        ``relevant`` board context reads (:class:`Relevance`).

        ``exclude_kinds`` leaves those kinds out INSIDE the query, as
        :meth:`filtered_events` does, so ``limit`` counts rows the caller can use.
        Dropped after the LIMIT, a run of the captain's audit lines filled the whole
        page, and the event behind it was not read at all (review of #240, finding 11).
        """
        keep, kept = _relevant_clause(relevant)
        rows = self._conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM team_event "
            "WHERE project_id = ? AND seq > ? "
            "AND (session_id IS NULL OR session_id != ?) "
            "AND (actor IS NULL OR actor != ?) "
            f"{_and_not_kinds(exclude_kinds)}{keep}ORDER BY seq LIMIT ?",
            (
                project_id,
                seq,
                exclude_session or "",
                exclude_actor or "",
                *exclude_kinds,
                *kept,
                limit,
            ),
        ).fetchall()
        return [_row_to_event(row) for row in rows]

    def recent_events(
        self,
        project_id: str,
        *,
        exclude_kinds: Collection[str] = (),
        relevant: Relevance | None = None,
        limit: int = 10,
    ) -> list[TeamEvent]:
        """A window: the NEWEST ``limit`` events, oldest first. ``exclude_kinds`` is left
        out inside the query, as in :meth:`events_since`: the window stays ``limit`` long
        however many of those kinds are newer than the events it shows. ``relevant`` as
        there."""
        keep, kept = _relevant_clause(relevant)
        rows = self._conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM team_event "
            f"WHERE project_id = ? {_and_not_kinds(exclude_kinds)}{keep}ORDER BY seq DESC LIMIT ?",
            (project_id, *exclude_kinds, *kept, limit),
        ).fetchall()
        return [_row_to_event(row) for row in reversed(rows)]

    def team_events_since(
        self,
        project_id: str,
        since: datetime,
        *,
        kinds: Sequence[str],
        human_kinds: Sequence[str] = (),
        session_kinds: Sequence[str] = (),
        sessions: Sequence[str] = (),
    ) -> list[TeamEvent]:
        """The project's events of ``kinds`` written at or after ``since``, those of
        ``human_kinds`` the human wrote (no session), and those of ``session_kinds`` that one
        of ``sessions`` wrote, oldest first.

        For a reader that runs every few seconds over the last day of a board (``remote``'s
        needs scan), however busy that day was: the ``(project_id, created_at)`` index
        narrows it to the events since ``since``, not the project's history. Not by ``seq``:
        the read started past the newest event stamped before ``since``, which took ``seq``
        order for stamp order, and an event written before the clock stepped back (NTP, a
        VM's snapshot) and stamped after ``since`` was skipped (review of #259, round 6,
        6/10). ``since`` compares as the stored ISO-8601 UTC strings do.
        """
        moment = since.astimezone(UTC).isoformat()
        wanted: list[str] = []
        params: list[str] = [project_id, moment]
        if kinds:
            wanted.append(f"kind IN ({', '.join('?' * len(kinds))})")
            params += kinds
        if human_kinds:
            wanted.append(f"(session_id IS NULL AND kind IN ({', '.join('?' * len(human_kinds))}))")
            params += human_kinds
        if session_kinds and sessions:
            wanted.append(
                f"(session_id IN ({', '.join('?' * len(sessions))}) "
                f"AND kind IN ({', '.join('?' * len(session_kinds))}))"
            )
            params += [*sessions, *session_kinds]
        if not wanted:
            return []
        rows = self._conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM team_event WHERE project_id = ? "
            f"AND created_at >= ? AND ({' OR '.join(wanted)}) ORDER BY seq",
            params,
        ).fetchall()
        return [_row_to_event(row) for row in rows]

    def newest_session_event(
        self, project_id: str, session_id: str, kind: str, *, since: datetime
    ) -> TeamEvent | None:
        """The session's newest event of ``kind`` written at or after ``since``; ``None``.

        For a reader that asks every few seconds (``remote``'s needs scan): the
        ``(project_id, created_at)`` index narrows it to the events since ``since``, so a
        session that has no such event costs those, not every event the project ever had.
        Not a walk back on ``seq`` that stops at the first event stamped before ``since``:
        after a clock stepped back, that one can be newer than the event asked for, which
        then went unseen (review of #259, round 6, 6/10). ``since`` compares as the stored
        ISO-8601 UTC strings do.
        """
        moment = since.astimezone(UTC).isoformat()
        row = self._conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM team_event WHERE project_id = ? AND session_id = ? "
            "AND kind = ? AND created_at >= ? ORDER BY seq DESC LIMIT 1",
            (project_id, session_id, kind, moment),
        ).fetchone()
        return None if row is None else _row_to_event(row)

    def latest_seq(self, project_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM team_event WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return int(row[0])

    # --- fleet -----------------------------------------------------------------

    def set_codename(self, project_id: str, codename: str) -> ProjectInfo:
        """Give a project its fleet codename (unique: the index raises on a clash)."""
        self._conn.execute("UPDATE project SET codename = ? WHERE id = ?", (codename, project_id))
        self._conn.commit()
        project = self.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        return project

    def set_project_nickname(self, project_id: str, nickname: str | None) -> ProjectInfo:
        """Name a project as a person sees it, or clear that name with ``None``.

        Written as given: the service (``services.project.set_nickname``) trims and
        bounds it. A forgotten project is a ``KeyError``, as an unknown one is, and
        as :meth:`update_project_layout` refuses one: a rename from a stale frame
        would otherwise land on the tombstone and come back with a revival.
        """
        cursor = self._conn.execute(
            "UPDATE project SET nickname = ? WHERE id = ? AND forgotten_at IS NULL",
            (nickname, project_id),
        )
        self._conn.commit()
        if cursor.rowcount != 1:
            raise KeyError(project_id)
        project = self.get_project(project_id)
        assert project is not None  # just written
        return project

    def codenames_in_use(self) -> set[str]:
        rows = self._conn.execute(
            "SELECT codename FROM project WHERE codename IS NOT NULL"
        ).fetchall()
        return {str(row["codename"]) for row in rows}

    def upsert_fleet_agent(self, agent: FleetAgent) -> FleetAgent:
        """Insert a fleet agent, or update the mutable facts of one already known.

        A second LIVE agent with the same label in the same project trips the
        partial unique index and raises ``sqlite3.IntegrityError`` — the service
        layer picks the suffixed label and retries; the store never guesses.

        The first message's state is written with the row and never by an update here:
        only its own transitions move it (:meth:`mark_prompted`,
        :meth:`mark_first_prompt_unsent`), so an upsert of a row read before its
        ``UserPromptSubmit`` cannot put an ``acked`` message back to ``pending``.
        """
        stored = self._upsert_fleet_agent(agent, None)
        assert stored is not None  # nothing guards it
        return stored

    def upsert_fleet_agent_unless_stopped(
        self, agent: FleetAgent, replaces: str
    ) -> FleetAgent | None:
        """:meth:`upsert_fleet_agent` for the row that brings ``replaces`` back: nothing is
        written, and ``None`` returned, when that row is recorded stopped by then. One
        statement, so an owner's ``fleet stop`` that lands at any point before the new row
        exists wins (last delta review of #274, m3)."""
        return self._upsert_fleet_agent(agent, replaces)

    def _upsert_fleet_agent(
        self, agent: FleetAgent, unless_stopped: str | None
    ) -> FleetAgent | None:
        guard = (
            "WHERE NOT EXISTS (SELECT 1 FROM fleet_agent WHERE id = ? AND ended_by = 'stopped') "
            if unless_stopped is not None
            else "WHERE true "  # INSERT … SELECT … ON CONFLICT needs a WHERE to parse
        )
        cursor = self._conn.execute(
            f"INSERT INTO fleet_agent ({_FLEET_AGENT_COLUMNS}) "
            "SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ? "
            f"{guard}"
            "ON CONFLICT (id) DO UPDATE SET "
            "pane_id = excluded.pane_id, session_id = excluded.session_id, "
            "cwd = excluded.cwd, worktree = excluded.worktree, task_id = excluded.task_id, "
            "ended_at = excluded.ended_at, exit_status = excluded.exit_status, "
            "account_slot = excluded.account_slot, launch_spec = excluded.launch_spec, "
            "socket_path = excluded.socket_path, boot_id = excluded.boot_id, "
            "server_started_at = excluded.server_started_at, branch = excluded.branch",
            (
                agent.id,
                agent.project_id,
                agent.label,
                agent.role,
                agent.binary,
                agent.tmux_socket,
                agent.pane_id,
                agent.session_id,
                str(agent.cwd),
                int(agent.worktree),
                agent.task_id,
                agent.spawned_by,
                agent.created_at.isoformat(),
                agent.ended_at.isoformat() if agent.ended_at else None,
                agent.exit_status,
                agent.account_slot,
                agent.launch_spec.model_dump_json() if agent.launch_spec is not None else None,
                str(agent.socket_path) if agent.socket_path is not None else None,
                agent.boot_id,
                agent.server_started_at.isoformat() if agent.server_started_at else None,
                agent.branch,
                agent.board if agent.board is not None else agent.project_id,
                agent.persona,
                agent.ended_by,
                agent.first_prompt,
                agent.first_prompt_at.isoformat() if agent.first_prompt_at else None,
                *((unless_stopped,) if unless_stopped is not None else ()),
            ),
        )
        self._conn.commit()
        if cursor.rowcount == 0:
            return None
        stored = self.get_fleet_agent(agent.id)
        assert stored is not None  # just written
        return stored

    def mark_prompted(self, session_id: str, at: datetime, *, agent_id: str | None = None) -> bool:
        """A prompt started a turn in ``session_id`` (``UserPromptSubmit``): stamp the session's
        ``prompted_at``, and take the first message of the fleet row it belongs to.

        The row is the one ``agent_id`` names (``AISQUARE_FLEET_AGENT``, the window's own) or,
        failing that, the live one bound to the session. Only a ``pending`` message is taken:
        one already ``acked`` keeps its time, and an ``unsent`` one becomes ``acked`` too,
        since a human's Enter sent it after the listing gave up on it. Returns whether this
        prompt took one. One transaction, an indexed read and at most two indexed updates:
        every prompt of every session pays it, and no row is read back.
        """
        self._conn.execute(
            "UPDATE team_session SET prompted_at = ? WHERE id = ?", (at.isoformat(), session_id)
        )
        return self.take_first_prompt(at, session_id=session_id, agent_id=agent_id)

    def take_first_prompt(
        self, at: datetime, *, session_id: str, agent_id: str | None = None
    ) -> bool:
        """Mark the first message of ``session_id``'s fleet row taken (``acked``), as
        :meth:`mark_prompted` does, without stamping ``prompted_at``: for a ``Stop``, which
        ends a turn that only a prompt starts. A ``UserPromptSubmit`` whose own record failed
        (a locked store; the hook fails open) left the row ``pending`` under a turn that ran,
        and a listing read it "first prompt not taken" (review of #330, round 1). Not the
        stamp: a tell's acknowledgement asks ``prompted_at``, and a Stop is no prompt.
        Returns whether this took one."""
        stamp = at.isoformat()
        # A read, then a compare-and-set on what it found: no ``RETURNING``, which the SQLite
        # of an older Python lacks.
        found = self._conn.execute(
            "SELECT id FROM fleet_agent "
            "WHERE first_prompt IN ('pending', 'unsent') AND ended_at IS NULL "
            "AND (id = ? OR (? IS NULL AND session_id = ?)) LIMIT 1",
            (agent_id, agent_id, session_id),
        ).fetchone()
        taken = found is not None and (
            self._conn.execute(
                "UPDATE fleet_agent SET first_prompt = 'acked', first_prompt_at = ? "
                "WHERE id = ? AND first_prompt IN ('pending', 'unsent')",
                (stamp, found["id"]),
            ).rowcount
            == 1
        )
        self._conn.commit()
        return taken

    def mark_first_prompt_unsent(self, agent_id: str, at: datetime) -> bool:
        """Move ``agent_id``'s first message from ``pending`` to ``unsent``: compare and set,
        so of two listings that both found it overdue exactly one moves it, and only that one
        writes the board's ``prompt_unsent``. False when it was not pending any more."""
        cursor = self._conn.execute(
            "UPDATE fleet_agent SET first_prompt = 'unsent', first_prompt_at = ? "
            "WHERE id = ? AND first_prompt = 'pending'",
            (at.isoformat(), agent_id),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def first_prompt_rows(self, since: datetime) -> list[FleetAgent]:
        """Every fleet row given a first message since ``since``, in any project: what doctor's
        ``fleet-delivery`` row counts."""
        rows = self._conn.execute(
            f"SELECT {self._fleet_agent_read} FROM fleet_agent "
            "WHERE first_prompt IS NOT NULL AND created_at >= ? ORDER BY created_at",
            (since.isoformat(),),
        ).fetchall()
        return [_row_to_fleet_agent(row) for row in rows]

    # --- project groups, pins and order (#140) ------------------------------------------

    @contextmanager
    def layout_change(self) -> Iterator[None]:
        """One change to the arrangement (a move, a group made or deleted, an undo) as ONE
        transaction.

        Each layout writer below commits on its own, and a change is several of
        them: a move renumbers the scope it leaves and the one it joins, and a
        group move renumbers every group. When the store refused a write part-way
        (a FOREIGN KEY failure on a group deleted from a shell since the change
        read it, a locked database), the rows written before it stayed committed.
        The layout was half-applied, and no undo entry was recorded for it,
        because the change raised (review of #203). Inside this block those
        writers leave the commit to the block: everything lands, or a raise rolls
        all of it back, the commit included. Blocks nest, and only the outermost
        one commits.
        """
        self._layout_depth += 1
        try:
            yield
            if self._layout_depth == 1:
                self._conn.commit()
        except BaseException:
            if self._layout_depth == 1:
                self._conn.rollback()
            raise
        finally:
            self._layout_depth -= 1

    def _commit_layout(self) -> None:
        """A layout writer's commit, unless a :meth:`layout_change` block makes it."""
        if not self._layout_depth:
            self._conn.commit()

    def project_groups(self) -> list[ProjectGroup]:
        rows = self._conn.execute(
            f"SELECT {_GROUP_COLUMNS} FROM project_group ORDER BY position, name"
        ).fetchall()
        return [_row_to_project_group(row) for row in rows]

    def get_project_group(self, group_id: str) -> ProjectGroup | None:
        row = self._conn.execute(
            f"SELECT {_GROUP_COLUMNS} FROM project_group WHERE id = ?", (group_id,)
        ).fetchone()
        return _row_to_project_group(row) if row is not None else None

    def find_project_group(self, name: str) -> ProjectGroup | None:
        """By exact name, then case-insensitively — names are unique either way."""
        row = self._conn.execute(
            f"SELECT {_GROUP_COLUMNS} FROM project_group WHERE name = ? "
            "OR lower(name) = lower(?) ORDER BY name = ? DESC LIMIT 1",
            (name, name, name),
        ).fetchone()
        return _row_to_project_group(row) if row is not None else None

    def create_project_group(self, name: str, *, group_id: str | None = None) -> ProjectGroup:
        """A new group at the END of the top level; a duplicate name raises ``ValueError``.

        ``group_id`` lets an undo re-create a deleted group under the id its
        members' rows were restored with.
        """
        if self.find_project_group(name) is not None:
            raise ValueError(f"a group named {name!r} already exists")
        last = self._conn.execute("SELECT MAX(position) FROM project_group").fetchone()[0]
        position = int(last) + 1 if last is not None else 0
        new_id = group_id or f"grp_{uuid.uuid4().hex[:24]}"
        try:
            self._conn.execute(
                f"INSERT INTO project_group ({_GROUP_COLUMNS}) VALUES (?, ?, ?, NULL, 0, ?)",
                (new_id, name, position, _now_iso()),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"a group named {name!r} already exists") from exc
        self._commit_layout()
        created = self.get_project_group(new_id)
        assert created is not None
        return created

    def update_project_group(
        self,
        group_id: str,
        *,
        name: str | None = None,
        position: int | None = None,
        pinned_at: datetime | EllipsisType | None = ...,
        collapsed: bool | None = None,
    ) -> ProjectGroup:
        """Change the given fields; ``...`` for ``pinned_at`` means "leave it" (``None`` unpins)."""
        sets: list[str] = []
        params: list[object] = []
        if name is not None:
            taken = self.find_project_group(name)
            if taken is not None and taken.id != group_id:
                raise ValueError(f"a group named {name!r} already exists")
            sets.append("name = ?")
            params.append(name)
        if position is not None:
            sets.append("position = ?")
            params.append(int(position))
        if pinned_at is not ...:
            sets.append("pinned_at = ?")
            params.append(pinned_at.isoformat() if pinned_at is not None else None)
        if collapsed is not None:
            sets.append("collapsed = ?")
            params.append(int(collapsed))
        if sets:
            try:
                cursor = self._conn.execute(
                    f"UPDATE project_group SET {', '.join(sets)} WHERE id = ?",
                    (*params, group_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"a group named {name!r} already exists") from exc
            self._commit_layout()
            if cursor.rowcount != 1:
                raise KeyError(group_id)
        updated = self.get_project_group(group_id)
        if updated is None:
            raise KeyError(group_id)
        return updated

    def delete_project_group(self, group_id: str) -> list[str]:
        """Delete the group; its members are ungrouped (never deleted). Returns their ids."""
        members = [
            str(row["id"])
            for row in self._conn.execute(
                "SELECT id FROM project WHERE group_id = ?", (group_id,)
            ).fetchall()
        ]
        self._conn.execute(
            "UPDATE project SET group_id = NULL, position = NULL WHERE group_id = ?", (group_id,)
        )
        cursor = self._conn.execute("DELETE FROM project_group WHERE id = ?", (group_id,))
        self._commit_layout()
        if cursor.rowcount != 1:
            raise KeyError(group_id)
        return members

    def group_settings(self, group_id: str) -> GroupSettings:
        """A group's parent, routing rules and cap (P2-W6, #199); none of them set when the
        group is gone."""
        row = self._conn.execute(
            "SELECT parent_project_id, route, max_agents FROM project_group WHERE id = ?",
            (group_id,),
        ).fetchone()
        if row is None:
            return GroupSettings(group_id=group_id)
        try:
            route = json.loads(row["route"]) if row["route"] else None
        except ValueError:
            route = None
        return GroupSettings(
            group_id=group_id,
            parent_project_id=row["parent_project_id"],
            route=route if isinstance(route, dict) else None,
            max_agents=int(row["max_agents"] or 0),
        )

    def set_group_settings(
        self,
        group_id: str,
        *,
        parent_project_id: str | EllipsisType | None = ...,
        route: Mapping[str, object] | EllipsisType | None = ...,
        max_agents: int | EllipsisType = ...,
    ) -> GroupSettings:
        """Change the given settings (``...`` leaves one as it is; ``None`` clears it). Raises
        ``KeyError`` for a group that is gone."""
        sets: list[str] = []
        params: list[object] = []
        if parent_project_id is not ...:
            sets.append("parent_project_id = ?")
            params.append(parent_project_id)
        if route is not ...:
            sets.append("route = ?")
            params.append(json.dumps(dict(route)) if route is not None else None)
        if max_agents is not ...:
            sets.append("max_agents = ?")
            params.append(int(max_agents))
        if sets:
            cursor = self._conn.execute(
                f"UPDATE project_group SET {', '.join(sets)} WHERE id = ?", (*params, group_id)
            )
            self._conn.commit()
            if cursor.rowcount != 1:
                raise KeyError(group_id)
        return self.group_settings(group_id)

    def set_task_origin(self, task_id: str, origin_task_id: str, origin_project_id: str) -> None:
        """Record the task ``task_id`` was delegated from (P2-W6): on another board, usually
        the group's parent."""
        self._conn.execute(
            "UPDATE team_task SET origin_task_id = ?, origin_project_id = ? WHERE id = ?",
            (origin_task_id, origin_project_id, task_id),
        )
        self._conn.commit()

    def task_origins(self, project_id: str) -> dict[str, tuple[str, str]]:
        """Of a board's tasks, the ones delegated from another: task id → (origin task id,
        origin project id)."""
        rows = self._conn.execute(
            "SELECT id, origin_task_id, origin_project_id FROM team_task "
            "WHERE project_id = ? AND origin_task_id IS NOT NULL",
            (project_id,),
        ).fetchall()
        return {str(r["id"]): (str(r["origin_task_id"]), str(r["origin_project_id"])) for r in rows}

    def linked_counts(self, origin_project_id: str) -> dict[str, tuple[int, int]]:
        """Of a board's tasks that others were delegated from: origin task id → (how many
        were delegated, how many of those are done). One query for the whole board."""
        rows = self._conn.execute(
            "SELECT origin_task_id, COUNT(*) AS n, SUM(status = 'done') AS d FROM team_task "
            "WHERE origin_project_id = ? AND origin_task_id IS NOT NULL GROUP BY origin_task_id",
            (origin_project_id,),
        ).fetchall()
        return {str(r["origin_task_id"]): (int(r["n"]), int(r["d"] or 0)) for r in rows}

    def linked_tasks(self, origin_task_id: str) -> list[TeamTask]:
        """Every task delegated from ``origin_task_id``, on any board."""
        rows = self._conn.execute(
            f"SELECT {_TASK_COLUMNS} FROM team_task WHERE origin_task_id = ? "
            "ORDER BY created_at, id",
            (origin_task_id,),
        ).fetchall()
        return [_row_to_task(row) for row in rows]

    def update_project_layout(
        self,
        project_id: str,
        *,
        group_id: str | EllipsisType | None = ...,
        position: int | EllipsisType | None = ...,
        pinned_at: datetime | EllipsisType | None = ...,
    ) -> ProjectInfo:
        """Change where a project sits — group, position, pin; ``...`` leaves a field alone.

        A forgotten project has no place to change: it is a ``KeyError``, as an
        unknown one is. A gesture from a stale frame — the sidebar between two
        refreshes, a drag, the group picker left open — while a shell ran
        ``project forget`` was written onto the tombstone, and the revival
        brought the project back pinned or grouped (review of #171, round 2).
        """
        sets: list[str] = []
        params: list[object] = []
        if group_id is not ...:
            sets.append("group_id = ?")
            params.append(group_id)
        if position is not ...:
            sets.append("position = ?")
            params.append(int(position) if position is not None else None)
        if pinned_at is not ...:
            sets.append("pinned_at = ?")
            params.append(pinned_at.isoformat() if pinned_at is not None else None)
        if sets:
            cursor = self._conn.execute(
                f"UPDATE project SET {', '.join(sets)} WHERE id = ? AND forgotten_at IS NULL",
                (*params, project_id),
            )
            self._commit_layout()
            if cursor.rowcount != 1:
                raise KeyError(project_id)
        updated = self._conn.execute(
            f"SELECT {_PROJECT_COLUMNS} FROM project WHERE id = ? AND forgotten_at IS NULL",
            (project_id,),
        ).fetchone()
        if updated is None:
            raise KeyError(project_id)
        return _row_to_project(updated)

    # --- a project's explainability key (#141) -----------------------------------------

    def project_explainability(self, project_id: str) -> ProjectExplainability | None:
        row = self._conn.execute(
            "SELECT project_id, target, key_path, set_at, set_by, api_url "
            "FROM project_explainability WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return _row_to_project_explainability(row) if row is not None else None

    def project_explainability_all(self) -> list[ProjectExplainability]:
        rows = self._conn.execute(
            "SELECT project_id, target, key_path, set_at, set_by, api_url "
            "FROM project_explainability ORDER BY project_id"
        ).fetchall()
        return [_row_to_project_explainability(row) for row in rows]

    def set_project_explainability(
        self,
        project_id: str,
        *,
        target: str,
        key_path: Path,
        set_by: str | None,
        minted: str | None = None,
        api_url: str | None = None,
    ) -> ProjectExplainability:
        """Attach (or re-point) the project's key: one row per project, the newest wins.

        The binding and the destination's ``key_uid`` describe one file, so they
        are written together (#142). ``minted`` is the uid of a key the CLI has
        just minted into that file; ``None`` is a key attached by hand. A minted
        key the file held before, other than ``minted``, has been overwritten:
        it is detached and its revocation owed in this same transaction
        (:meth:`_owe_revocation`), so a hand key never goes on being described
        as minted and the key it replaced is never forgotten while still live.
        ``minted`` itself, if it was still owed, is owed no more
        (:meth:`_owe_no_revocation`): it is the project's key again.

        ``api_url`` is the destination's API when the key is for the deployment
        that destination names, and ``None`` for one of the machine's targets
        (:attr:`ProjectExplainability.api_url`, v24). Every attach writes it, so
        a re-point never keeps the earlier key's.

        THE COMMIT IS THE LAST THING THAT CAN FAIL. The binding returned is
        built from the values written, not read back after the commit. Both
        callers (``attach_project_key`` and ``destinations.mint_key``) take a
        raise from here to mean "nothing was recorded" and put the key file
        back as it was. A read-back that failed after the commit put the
        earlier key under a row already committed to the new target: a prod
        binding answering with the stg key (review of #170, D1b round 2, B2).
        """
        now = _now_iso()
        stored = ProjectExplainability(
            project_id=project_id,
            target=target,
            key_path=Path(str(key_path)),
            set_at=datetime.fromisoformat(now),
            set_by=set_by,
            api_url=api_url,
        )
        with self._conn:
            self._conn.execute(
                "INSERT INTO project_explainability (project_id, target, key_path, set_at, "
                "set_by, api_url) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (project_id) DO UPDATE SET target = excluded.target, "
                "key_path = excluded.key_path, set_at = excluded.set_at, "
                "set_by = excluded.set_by, api_url = excluded.api_url",
                (project_id, target, str(key_path), now, set_by, api_url),
            )
            self._owe_revocation(project_id, keep=minted)
            if minted is not None:
                self._conn.execute(
                    "UPDATE project_destination SET key_uid = ? WHERE project_id = ?",
                    (minted, project_id),
                )
                self._owe_no_revocation(project_id, minted)
        return stored

    def clear_project_explainability(self, project_id: str) -> bool:
        """Detach the project's key; ``False`` when there was none.

        A key the CLI minted goes with its binding, its revocation owed in the
        same transaction (:meth:`_owe_revocation`).
        """
        with self._conn:
            self._owe_revocation(project_id)
            cursor = self._conn.execute(
                "DELETE FROM project_explainability WHERE project_id = ?", (project_id,)
            )
        return cursor.rowcount > 0

    # --- where a project's traces land (#142) -------------------------------------------

    def project_destination(self, project_id: str) -> TraceDestination | None:
        row = self._conn.execute(
            f"SELECT {_DESTINATION_COLUMNS} FROM project_destination WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return _row_to_trace_destination(row) if row is not None else None

    def project_destinations(self) -> list[TraceDestination]:
        rows = self._conn.execute(
            f"SELECT {_DESTINATION_COLUMNS} FROM project_destination ORDER BY project_id"
        ).fetchall()
        return [_row_to_trace_destination(row) for row in rows]

    def set_project_destination(self, destination: TraceDestination) -> TraceDestination:
        """Record (or re-point) where the project's traces land: one row per project.

        ``set_at`` is stamped here, not trusted from the caller, so the row says
        when the choice was made on THIS machine. A re-point keeps ``key_uid``
        only when the caller carries it over — a destination in another
        workspace is not served by a key minted for the old one — and a minted
        key it does not carry over is detached, its binding with it, and its
        revocation owed in the same transaction (:meth:`_owe_revocation`).
        """
        with self._conn:
            if self._owe_revocation(destination.project_id, keep=destination.key_uid) and (
                destination.key_uid is None
            ):
                self._drop_binding(destination.project_id)
            self._conn.execute(
                f"INSERT INTO project_destination ({_DESTINATION_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (project_id) DO UPDATE SET api_url = excluded.api_url, "
                "environment = excluded.environment, workspace_id = excluded.workspace_id, "
                "workspace_uid = excluded.workspace_uid, "
                "workspace_name = excluded.workspace_name, "
                "studio_id = excluded.studio_id, studio_uid = excluded.studio_uid, "
                "studio_name = excluded.studio_name, key_uid = excluded.key_uid, "
                "set_at = excluded.set_at, set_by = excluded.set_by",
                (
                    destination.project_id,
                    destination.api_url,
                    destination.environment,
                    destination.workspace_id,
                    destination.workspace_uid,
                    destination.workspace_name,
                    destination.studio_id,
                    destination.studio_uid,
                    destination.studio_name,
                    destination.key_uid,
                    _now_iso(),
                    destination.set_by,
                ),
            )
            if destination.key_uid is not None:
                self._owe_no_revocation(destination.project_id, destination.key_uid)
        stored = self.project_destination(destination.project_id)
        assert stored is not None  # just written
        return stored

    def set_project_destination_key(self, project_id: str, key_uid: str | None) -> None:
        """Remember the ingest key the CLI minted for this destination; ``None``: it has none.

        The uid it replaces is owed a revocation, in the same transaction
        (:meth:`_owe_revocation`); with ``None`` the key's binding goes too, as
        :meth:`detach_minted_key` takes it.
        """
        with self._conn:
            detached = self._owe_revocation(project_id, keep=key_uid)
            if key_uid is not None:
                self._conn.execute(
                    "UPDATE project_destination SET key_uid = ? WHERE project_id = ?",
                    (key_uid, project_id),
                )
                self._owe_no_revocation(project_id, key_uid)
            elif detached:
                self._drop_binding(project_id)

    def detach_minted_key(self, project_id: str) -> tuple[bool, ProjectExplainability | None]:
        """Take the key the CLI minted off the project: its revocation owed, its binding gone.

        One transaction: the destination's ``key_uid`` is cleared, the uid is
        written to ``pending_revocation``, and the binding that named the key's
        file is deleted — a binding left on a key about to be revoked is a
        project whose launches authenticate with a dead key. Returns whether a
        key was detached, and that binding, whose file the caller deletes
        (``None`` when there was none). Two facts, not one: a uid with no
        binding left is detached all the same, and counting detachments by the
        binding undercounted what ``logout`` forgot (review of #172's
        follow-ups, round 1, F5).
        """
        with self._conn:
            if not self._owe_revocation(project_id):
                return False, None
            binding = self.project_explainability(project_id)
            self._drop_binding(project_id)
        return True, binding

    def clear_project_destination(self, project_id: str) -> bool:
        """Forget where the project's traces land; ``False`` when nothing was recorded.

        A minted key goes with the row, as :meth:`detach_minted_key` takes it.
        """
        with self._conn:
            if self._owe_revocation(project_id):
                self._drop_binding(project_id)
            cursor = self._conn.execute(
                "DELETE FROM project_destination WHERE project_id = ?", (project_id,)
            )
        return cursor.rowcount > 0

    def _owe_revocation(self, project_id: str, *, keep: str | None = None) -> bool:
        """Inside the caller's transaction: detach the project's minted key, owing its revocation.

        THE ONE PLACE a minted key's uid leaves its destination row. Every write
        that takes it off — the binding replaced or cleared, the row re-pointed,
        re-keyed or deleted, the project purged — calls this first, in its own
        transaction, so the uid moves to ``pending_revocation`` in the same
        commit that detaches it: no crash, lock or interrupt between two writes
        can leave a live key that nothing here remembers, and no caller can
        detach one without owing it. The revoke itself is a network call and
        happens after the store session closes (``destinations.revoke_owed``),
        which deletes the record only on the server's confirmation.

        ``keep`` is a uid the row may go on naming (the key a new mint just
        stored, a re-point within the workspace): nothing is detached then. A
        uid that never arrived (:data:`UNKNOWN_KEY_UID`) is detached with nothing
        owed, since nothing could revoke it. The INSERT goes first: as the
        transaction's first write it takes the write lock, so the row it reads
        cannot change under it. Returns whether a key was detached.
        """
        self._conn.execute(
            "INSERT INTO pending_revocation (key_uid, api_url, workspace_id, workspace_name, "
            "project_id, project_name, detached_at) "
            "SELECT d.key_uid, d.api_url, d.workspace_id, d.workspace_name, d.project_id, "
            "COALESCE(p.name, d.project_id), ? "
            "FROM project_destination AS d LEFT JOIN project AS p ON p.id = d.project_id "
            "WHERE d.project_id = ? AND d.key_uid IS NOT NULL AND d.key_uid IS NOT ? "
            "AND d.key_uid <> ? "
            "ON CONFLICT (key_uid) DO NOTHING",
            (_now_iso(), project_id, keep, UNKNOWN_KEY_UID),
        )
        cursor = self._conn.execute(
            "UPDATE project_destination SET key_uid = NULL "
            "WHERE project_id = ? AND key_uid IS NOT NULL AND key_uid IS NOT ?",
            (project_id, keep),
        )
        return cursor.rowcount > 0

    def _owe_no_revocation(self, project_id: str, key_uid: str) -> None:
        """Inside the caller's transaction: ``key_uid`` is the project's key again, so owed nothing.

        The other half of :meth:`_owe_revocation`, called by every write that
        stores a uid on a destination row. A uid still owed can come back — a
        mint that answers with the uid it answered before, a re-point that
        carries the row's uid over — and left owed, the same command's
        ``revoke_owed`` revoked the key the project had just been given
        (review of #172's follow-ups, round 1, F2). Only while the row names
        it: a record whose uid no row holds is still owed.
        """
        self._conn.execute(
            "DELETE FROM pending_revocation WHERE key_uid = ? AND EXISTS "
            "(SELECT 1 FROM project_destination WHERE project_id = ? AND key_uid = ?)",
            (key_uid, project_id, key_uid),
        )

    def _drop_binding(self, project_id: str) -> None:
        self._conn.execute("DELETE FROM project_explainability WHERE project_id = ?", (project_id,))

    # --- revocations owed for keys the CLI minted (v22, #142) -----------------------------

    def pending_revocations(self) -> list[PendingRevocation]:
        """Every minted key detached from its project and not confirmed revoked, oldest first."""
        rows = self._conn.execute(
            "SELECT key_uid, api_url, workspace_id, workspace_name, project_id, project_name, "
            "detached_at, last_error FROM pending_revocation ORDER BY detached_at, key_uid"
        ).fetchall()
        return [
            PendingRevocation(
                key_uid=row["key_uid"],
                api_url=row["api_url"],
                workspace_id=int(row["workspace_id"]),
                workspace_name=row["workspace_name"],
                project_id=row["project_id"],
                project_name=row["project_name"],
                detached_at=datetime.fromisoformat(row["detached_at"]),
                last_error=row["last_error"],
            )
            for row in rows
        ]

    def settle_revocation(self, key_uid: str) -> bool:
        """The server confirmed the key is revoked: nothing is owed for it any more."""
        cursor = self._conn.execute("DELETE FROM pending_revocation WHERE key_uid = ?", (key_uid,))
        self._conn.commit()
        return cursor.rowcount > 0

    def note_revocation_failure(self, key_uid: str, reason: str) -> None:
        """Why the last attempt to revoke ``key_uid`` did not: what every report of it says."""
        self._conn.execute(
            "UPDATE pending_revocation SET last_error = ? WHERE key_uid = ?", (reason, key_uid)
        )
        self._conn.commit()

    # --- UI state (#144) ---------------------------------------------------------------

    def ui_state(self, key: str) -> str | None:
        """The remembered UI fact under ``key``, or ``None``."""
        row = self._conn.execute("SELECT value FROM ui_state WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row is not None else None

    def set_ui_state(self, key: str, value: str | None) -> None:
        """Remember ``value`` under ``key``; ``None`` forgets it. Every change is the save."""
        if value is None:
            self._conn.execute("DELETE FROM ui_state WHERE key = ?", (key,))
        else:
            self._conn.execute(
                "INSERT INTO ui_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, value, _now_iso()),
            )
        self._conn.commit()

    def replace_ui_state(self, key: str, expected: str | None, value: str | None) -> bool:
        """Compare-and-set: write ``value`` under ``key`` (``None`` forgets it) only while the
        row still holds ``expected`` (``None``: no row). Returns whether it was written.

        For a fact two processes may race on, read in one session and written in another
        (a usage read's back-off, F1 of #400): a stale write must not undo a newer one."""
        if expected is None and value is None:
            row = self._conn.execute("SELECT 1 FROM ui_state WHERE key = ?", (key,)).fetchone()
            return row is None
        if expected is None:
            cursor = self._conn.execute(
                "INSERT INTO ui_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (key) DO NOTHING",
                (key, value, _now_iso()),
            )
        elif value is None:
            cursor = self._conn.execute(
                "DELETE FROM ui_state WHERE key = ? AND value = ?", (key, expected)
            )
        else:
            cursor = self._conn.execute(
                "UPDATE ui_state SET value = ?, updated_at = ? WHERE key = ? AND value = ?",
                (value, _now_iso(), key, expected),
            )
        self._conn.commit()
        return cursor.rowcount == 1

    def get_fleet_agent(self, ref: str) -> FleetAgent | None:
        """A fleet agent by id or unambiguous id prefix (git-style)."""
        rows = self._conn.execute(
            f"SELECT {self._fleet_agent_read} FROM fleet_agent WHERE id GLOB ? LIMIT 2",
            (_glob_prefix(ref),),
        ).fetchall()
        if len(rows) > 1:
            raise AmbiguousIdError(ref)
        return _row_to_fleet_agent(rows[0]) if rows else None

    def fleet_agents(self, project_id: str, *, live_only: bool = False) -> list[FleetAgent]:
        clause = " AND ended_at IS NULL" if live_only else ""
        rows = self._conn.execute(
            f"SELECT {self._fleet_agent_read} FROM fleet_agent "
            f"WHERE project_id = ?{clause} ORDER BY created_at, id",
            (project_id,),
        ).fetchall()
        return [_row_to_fleet_agent(row) for row in rows]

    def live_fleet_agents(self) -> list[FleetAgent]:
        """Every live fleet row, in every project, forgotten ones' included, oldest first.

        One query, for a question about the tmux server every project shares (whose rows a
        server started at a socket path would answer for): asked project by project, it
        ran one query per project ever registered, captured directories included, and a
        spawn asks it twice (review of #259, round 4)."""
        rows = self._conn.execute(
            f"SELECT {self._fleet_agent_read} FROM fleet_agent "
            "WHERE ended_at IS NULL ORDER BY created_at, id"
        ).fetchall()
        return [_row_to_fleet_agent(row) for row in rows]

    def fleet_label_heads(self, project_id: str | None = None) -> list[FleetAgent]:
        """Every live fleet row, and the newest row of each label, live or ended, in every
        project, forgotten ones' included, oldest first: the rows a ``fleet stop`` or a
        ``restart`` by label acts on (:meth:`fleet_agent_by_label`), ended or not.

        The rows that may read a board again (``services.project.reading_rows``): a restart
        of a stopped one starts its replacement under the hub its launch recorded. Picked in
        SQL: every spawn, restart and switch adds a row that is never deleted.

        Each label's newest by a window over the table, one pass and a sort. A correlated
        ``NOT EXISTS`` asked for a newer row of the label per row, over an index with no
        ``label``, and was quadratic in a project's rows: 4.6 s at 20 000, on every
        ``project forget`` and ``prune`` and doctor's capture-off line at each ``asq`` start
        (review of #259, round 37, 4/7). Two rows of a label stamped alike, which that
        query gave both of, give the one of the greater id.

        ``project_id`` keeps it to one project's rows, in the query: a restart's plan asks
        for one project, once per project and again per row (final delta review, n5)."""
        scope, args = ("WHERE project_id = ?", (project_id,)) if project_id else ("", ())
        rows = self._conn.execute(
            f"SELECT {self._fleet_agent_read} FROM fleet_agent WHERE id IN ("
            "SELECT id FROM (SELECT id, ended_at, ROW_NUMBER() OVER ("
            "PARTITION BY project_id, label ORDER BY created_at DESC, id DESC) AS newest "
            f"FROM fleet_agent {scope}) WHERE ended_at IS NULL OR newest = 1) "
            "ORDER BY created_at, id",
            args,
        ).fetchall()
        return [_row_to_fleet_agent(row) for row in rows]

    def fleet_live_counts(self, since: datetime) -> dict[str, int]:
        """Project id → its live rows, for every project with a live row or one that ended at
        or after ``since`` (``0`` for one with only the latter); no other project is a key.

        The projects a listing of recent rows can show anything for (``services.fleet``'s
        ``read_listings``, ``asq``'s tick, #83): with 305 listed projects every tick read
        every one's rows and sessions, and asked its tmux server, for no agent at all.
        One pass over the table, grouped in SQL.
        """
        rows = self._conn.execute(
            "SELECT project_id, SUM(ended_at IS NULL) FROM fleet_agent "
            "WHERE ended_at IS NULL OR ended_at >= ? GROUP BY project_id",
            (since.astimezone(UTC).isoformat(),),
        ).fetchall()
        return {row[0]: int(row[1]) for row in rows}

    def fleet_agents_ended_since(self, project_id: str, since: datetime) -> list[FleetAgent]:
        """The project's rows that ended at or after ``since``, oldest first.

        Filtered here, not by the caller: every spawn, restart and switch adds a row that is
        never deleted, and a reader that runs every few seconds for the last day's endings
        (``remote``'s needs scan) built every row the project ever had to keep a handful.
        ``since`` compares as the stored ISO-8601 UTC strings do.
        """
        rows = self._conn.execute(
            f"SELECT {self._fleet_agent_read} FROM fleet_agent "
            "WHERE project_id = ? AND ended_at >= ? ORDER BY created_at, id",
            (project_id, since.astimezone(UTC).isoformat()),
        ).fetchall()
        return [_row_to_fleet_agent(row) for row in rows]

    def fleet_agent_boards(self, project_id: str, *, live_only: bool = False) -> list[str]:
        """Every board one of the project's rows recorded, live or ended (``live_only``: live),
        sorted; ``[]`` before its first spawn.

        The boards a phone may reach for the project (``services.remote_server``'s
        ``_remote_boards``); the live ones, which a pause goes to besides its manager's and
        its homes (``services.fleet._pause_boards``), whether the project ever had a manager
        row or not. Distinct in SQL: every spawn, restart
        and switch adds a row that is never deleted, and :meth:`fleet_agents` builds them all.
        """
        clause = " AND ended_at IS NULL" if live_only else ""
        rows = self._conn.execute(
            f"SELECT DISTINCT {self._fleet_agent_board} AS board FROM fleet_agent "
            f"WHERE project_id = ?{clause} ORDER BY board",
            (project_id,),
        ).fetchall()
        return [row["board"] for row in rows]

    def fleet_board(self, project_id: str) -> str | None:
        """The board the project's running agents read, as one live row recorded it: its live
        manager's, else its newest live row's; ``None`` with none live.

        Picked in SQL (``services.remote_server``'s ``_remote_project_board``): every spawn,
        restart and switch adds a row that is never deleted, and :meth:`fleet_agents` builds
        them all. Not an ended row's: a project whose agents had all stopped kept the board
        of its last spawn, however long ago, and under a hub that spawn was given, Remote's
        ``done #1`` closed the hub's #1, not the one ``aisquare board`` shows in the project
        (review of #259, round 30, 1/8).
        """
        row = self._conn.execute(
            f"SELECT {self._fleet_agent_board} AS board FROM fleet_agent "
            "WHERE project_id = ? AND ended_at IS NULL "
            "ORDER BY role = 'manager' DESC, created_at DESC, id DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        return None if row is None else str(row["board"])

    def fleet_manager_board(self, project_id: str) -> str | None:
        """The board the project's newest manager row recorded, a live one first: the board
        its manager reads, or the one a ``restart`` of its last one hands the replacement
        (``services.fleet._board_and_hub``); ``None`` before its first manager.

        Where a pause is set besides its homes and the boards its live rows recorded
        (``services.fleet._pause_boards``). Picked in SQL, as :meth:`fleet_board` is.
        """
        row = self._conn.execute(
            f"SELECT {self._fleet_agent_board} AS board FROM fleet_agent "
            "WHERE project_id = ? AND role = 'manager' "
            "ORDER BY ended_at IS NULL DESC, created_at DESC, id DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        return None if row is None else str(row["board"])

    def fleet_agent_for_session(self, session_id: str) -> FleetAgent | None:
        """The live fleet row recorded against ``session_id``, if there is one, in whichever
        project it is.

        One targeted lookup rather than materialising every live row and
        scanning: this runs on the session-start hook AND on every ``task next``,
        including the plain CLI ones that have no fleet row at all and paid for
        the whole list to find that out (review of #116, round 3). It walks the live
        rows alone, through the partial ``fleet_agent_live_label`` index.

        Not filtered on the session's project: under ``AISQUARE_TEAM_HUB`` the session is
        on the hub's board while its row is in the project it was spawned in, and the
        limit hand-over, asking with the board's project, found no row and never switched
        the agent (review of #259, round 23). A session id names one session wherever it
        sits, so the row recorded against it is that session's.

        Two live rows on one session stamped alike (a resume that keeps the session id
        before the old row ends) give the one of the greater id, as :meth:`fleet_agent_by_label`
        and :meth:`fleet_label_heads` pick: with no tie-break SQLite gave whichever its plan
        reached first, and the hand-over could act on another row than a ``fleet stop`` or
        ``restart`` by label (review of #259, round 42, 7/11).
        """
        row = self._conn.execute(
            f"SELECT {self._fleet_agent_read} FROM fleet_agent "
            "WHERE session_id = ? AND ended_at IS NULL "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return _row_to_fleet_agent(row) if row is not None else None

    def adopt_fleet_agent_session(
        self, agent_id: str, previous: str | None, session_id: str, lease_until: datetime
    ) -> bool:
        """Move a LIVE fleet row from ``previous`` to ``session_id``, claims and all.

        ``False`` when nothing moved. One transaction for the two facts that must
        agree: which session the row is, and who holds the tasks that session
        claimed. A ``/clear`` mints a new session id for the process that is
        still working them, so every claim the old id holds — the assigned task
        and any pool task it took, in every status that keeps a claim
        (:data:`CLAIM_KEEPING_STATUSES`) — is re-addressed to the new id, and
        the ``doing`` ones take the new lease. Committed separately they could disagree:
        a row bound to the new id while the work stayed with the old one, whose
        lease then ran out under a working agent (review of #135).

        ``previous`` is part of the WHERE (``IS``, so an unbound row matches
        ``None``): the hook that calls this runs in another process from
        ``fleet stop`` and from every other hook, so the row must still be the
        one that was read — ended, or taken by somebody else in between, and
        nothing moves. A targeted UPDATE, never a whole-row write, for the same
        reason (a stale snapshot written back resurrected a stopped agent).
        """
        try:
            cursor = self._conn.execute(
                "UPDATE fleet_agent SET session_id = ? "
                "WHERE id = ? AND ended_at IS NULL AND session_id IS ?",
                (session_id, agent_id, previous),
            )
            if cursor.rowcount != 1:
                self._conn.rollback()
                return False
            if previous is not None:
                # Every claim the old id holds moves; the LEASE is stamped on the
                # ``doing`` ones alone. Stamped on a review or blocked task too,
                # it was a lease nothing renews (``renew_leases``) and nothing
                # reclaims (``claim_task``): "lease until 14:07" on the board
                # for a claim that had lapsed and could not be taken (review of
                # #203). One named set for the statuses that keep a claim.
                statuses = tuple(sorted(CLAIM_KEEPING_STATUSES))
                self._conn.execute(
                    "UPDATE team_task SET claimed_by = ?, updated_at = ?, "
                    "claim_expires_at = CASE WHEN status = 'doing' THEN ? "
                    "ELSE claim_expires_at END "
                    f"WHERE claimed_by = ? AND status IN ({', '.join('?' * len(statuses))})",
                    (session_id, _now_iso(), lease_until.isoformat(), previous, *statuses),
                )
        except Exception:
            self._conn.rollback()
            raise
        self._conn.commit()
        return True

    def retire_fleet_assignments(self, task_id: str) -> int:
        """Forget ``task_id`` on every live fleet row spawned for it; how many did.

        An assignment is over when its task is closed. A row that kept naming a
        done task re-briefed its agent on every later session start — "already
        done; tell the manager" — and a nudge went out to the manager each time
        for nothing; reopened and claimed by someone else, the same row ordered
        a busy agent to stand down (review of #135). What the operator sees: the
        row no longer names the task — the agent is no longer on it — while the
        label (``coder-<task>``) and the branch keep the task's short id, so the
        row still says what it was spawned for (review of #135, second round,
        finding 7). The agent info line shows no task at all since #267: the
        label is the tie there.
        """
        cursor = self._conn.execute(
            "UPDATE fleet_agent SET task_id = NULL WHERE task_id = ? AND ended_at IS NULL",
            (task_id,),
        )
        self._conn.commit()
        return cursor.rowcount

    def fleet_agent_by_label(
        self, project_id: str, label: str, *, live_only: bool = True
    ) -> FleetAgent | None:
        """The newest row of ``label`` in the project (``live_only``: its live one), two
        stamped alike giving the one of the greater id, as :meth:`fleet_label_heads` picks
        it. With no tie-break, SQLite gave whichever its plan reached first, and the holds
        ``project forget`` and prune judge could read one row while a restart acted on the
        other (review of #259, round 41, 4/6)."""
        clause = " AND ended_at IS NULL" if live_only else ""
        row = self._conn.execute(
            f"SELECT {self._fleet_agent_read} FROM fleet_agent "
            f"WHERE project_id = ? AND label = ?{clause} ORDER BY created_at DESC, id DESC "
            "LIMIT 1",
            (project_id, label),
        ).fetchone()
        return _row_to_fleet_agent(row) if row is not None else None

    def end_fleet_agent(
        self, agent_id: str, *, exit_status: int | None = None, ended_by: EndedBy
    ) -> FleetAgent:
        """Mark an agent ended (idempotent: an already-ended row keeps its first end)."""
        self.end_fleet_agent_if_live(agent_id, exit_status=exit_status, ended_by=ended_by)
        agent = self.get_fleet_agent(agent_id)
        if agent is None:
            raise KeyError(agent_id)
        return agent

    def set_fleet_agent_persona(self, agent_id: str, persona: str | None) -> FleetAgent:
        """The persona a running agent was given (``fleet.attach_persona``, plan §4.7)."""
        self._conn.execute("UPDATE fleet_agent SET persona = ? WHERE id = ?", (persona, agent_id))
        self._conn.commit()
        agent = self.get_fleet_agent(agent_id)
        if agent is None:
            raise KeyError(agent_id)
        return agent

    def set_session_persona(self, session_id: str, persona: str | None) -> TeamSession:
        """The persona a joined session now runs as — beside ``set_fleet_agent_persona``."""
        session = self.get_session(session_id)
        if session is None:
            raise KeyError(session_id)
        self._conn.execute(
            "UPDATE team_session SET persona = ? WHERE id = ?", (persona, session.id)
        )
        self._conn.commit()
        updated = self.get_session(session.id)
        assert updated is not None  # just updated
        return updated

    def end_fleet_agent_if_live(
        self, agent_id: str, *, exit_status: int | None = None, ended_by: EndedBy
    ) -> FleetAgent | None:
        """End a LIVE agent's row and return it; ``None`` when it was already ended.

        ``ended_by`` is how (v34, #38): ``stopped`` on aisquare's word, ``died`` otherwise.

        The UPDATE is a compare-and-set on ``ended_at IS NULL``, for a caller that
        announces the end: every fleet read records a dead pane (#138), so two
        readers routinely see one death, and only the one whose write ended the
        row may post ``agent_exited``. A re-read before the write left a statement
        for the other to land in (review of #138).
        """
        cursor = self._conn.execute(
            "UPDATE fleet_agent SET ended_at = ?, exit_status = ?, ended_by = ? "
            "WHERE id = ? AND ended_at IS NULL",
            (_now_iso(), exit_status, ended_by, agent_id),
        )
        self._conn.commit()
        if cursor.rowcount == 0:
            return None
        return self.get_fleet_agent(agent_id)

    def mark_fleet_agent_stopped(self, agent_id: str) -> FleetAgent:
        """Record an ENDED row as stopped on aisquare's word (#38): a 💤 row whose window
        ``fleet stop`` removed, which Restart all must then leave. A live row is untouched:
        its end is written with how it ended."""
        self._conn.execute(
            "UPDATE fleet_agent SET ended_by = 'stopped' WHERE id = ? AND ended_at IS NOT NULL",
            (agent_id,),
        )
        self._conn.commit()
        agent = self.get_fleet_agent(agent_id)
        if agent is None:
            raise KeyError(agent_id)
        return agent

    def forget_fleet_agent_server_start(self, agent_id: str, started: datetime) -> bool:
        """Clear a LIVE row's recorded server start while it is still ``started``; whether it did.

        For a row whose server exited (``services.fleet._forget_an_exited_server``): with no
        start recorded, it reads lost under the next server at its socket, as a row from
        before 0.8.1 does, rather than held as that server's for good. A compare-and-set on
        the start the caller read and on ``ended_at IS NULL``, so a row ended or rewritten
        since is left as it is.
        """
        cursor = self._conn.execute(
            "UPDATE fleet_agent SET server_started_at = NULL "
            "WHERE id = ? AND ended_at IS NULL AND server_started_at = ?",
            (agent_id, started.isoformat()),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def record_fleet_agent_branch(self, agent_id: str, branch: str | None) -> None:
        """Record the branch a fleet agent's worktree is on now (``None``: a detached HEAD).

        Whether the row is live or ended: ``services.fleet`` records it at stop and at reap,
        before the tree can go, and a restart makes a gone tree again on it."""
        self._conn.execute("UPDATE fleet_agent SET branch = ? WHERE id = ?", (branch, agent_id))
        self._conn.commit()

    # --- the Claude account registry (v15, #145) --------------------------------------------
    #
    # Every write here is one statement and one commit, and every invariant that
    # matters — one default, unique aliases — is an index the statement trips
    # rather than a check the caller remembers. The service layer decides WHICH
    # slot; this layer only refuses what the schema refuses.

    def claude_accounts(self) -> list[ClaudeAccountRecord]:
        """Every registered slot in priority order (position, then slot)."""
        rows = self._conn.execute(
            f"SELECT {_CLAUDE_ACCOUNT_COLUMNS} FROM claude_account ORDER BY position, slot"
        ).fetchall()
        return [_row_to_claude_account(row) for row in rows]

    def upsert_claude_account(self, slot: int, config_dir: Path) -> ClaudeAccountRecord:
        """Register ``slot`` at the END of the order, or refresh a known slot's directory.

        The arrangement of a known slot — alias, position, disabled —
        is never touched by an upsert: a sync that re-reads the directories
        must not reorder what the operator arranged. New slots queue at the
        end because "the account I just added" is the one most safely ranked
        last; ``order_claude_accounts`` moves it wherever it belongs.
        """
        self._conn.execute(
            f"INSERT INTO claude_account ({_CLAUDE_ACCOUNT_COLUMNS}) VALUES "
            "(?, ?, NULL, COALESCE((SELECT MAX(position) FROM claude_account), 0) + 1, 0, 0, ?) "
            "ON CONFLICT (slot) DO UPDATE SET config_dir = excluded.config_dir",
            (slot, str(config_dir), _now_iso()),
        )
        self._conn.commit()
        row = self._conn.execute(
            f"SELECT {_CLAUDE_ACCOUNT_COLUMNS} FROM claude_account WHERE slot = ?", (slot,)
        ).fetchone()
        assert row is not None  # just written
        return _row_to_claude_account(row)

    def delete_claude_account(self, slot: int) -> bool:
        """Forget a slot's arrangement; True when there was one.

        Its rank goes with the row and the order closes up, so the next ``add``,
        which reuses the number, queues at the end rather than inheriting a place.
        """
        return self.delete_claude_accounts([slot]) == 1

    def delete_claude_accounts(self, slots: Sequence[int]) -> int:
        """Forget several slots' arrangements at once; how many rows there were.

        The order is renumbered ONCE, after every delete, in the same
        transaction: the reconcile prunes every vanished slot in one pass, and
        deleted one at a time each row paid a full renumbering — k slots, k
        passes over the table (review of #205, fourth round).
        """
        deleted = 0
        for slot in slots:
            cursor = self._conn.execute("DELETE FROM claude_account WHERE slot = ?", (slot,))
            deleted += cursor.rowcount
        if deleted:
            self.order_claude_accounts([])  # close the gaps the rows leave; commits all of it
        else:
            self._conn.commit()
        return deleted

    def set_claude_account_alias(self, slot: int, alias: str | None) -> None:
        """Name a slot (``None`` unnames it). A taken alias raises ``sqlite3.IntegrityError``."""
        cursor = self._conn.execute(
            "UPDATE claude_account SET alias = ? WHERE slot = ?", (alias, slot)
        )
        if cursor.rowcount != 1:
            self._conn.rollback()
            raise KeyError(slot)
        self._conn.commit()

    def set_claude_account_disabled(self, slot: int, disabled: bool) -> None:
        cursor = self._conn.execute(
            "UPDATE claude_account SET disabled = ? WHERE slot = ?", (int(disabled), slot)
        )
        if cursor.rowcount != 1:
            self._conn.rollback()
            raise KeyError(slot)
        self._conn.commit()

    def order_claude_accounts(self, slots: Sequence[int]) -> None:
        """Rewrite the positions: ``slots`` first, in that order, then everything else as it was.

        Positions are renumbered 1..n on every call, so the order is always a
        dense sequence and never accumulates gaps or ties. Unknown slots in
        ``slots`` are ignored — the directories are the record, and the caller
        already resolved what exists.
        """
        current = [record.slot for record in self.claude_accounts()]
        # De-duplicated, first mention wins: `order 2 2 3` wrote slot 2 twice and
        # left position 1 unused, which is the gap the docstring rules out.
        wanted = list(dict.fromkeys(slot for slot in slots if slot in current))
        rest = [slot for slot in current if slot not in wanted]
        for position, slot in enumerate([*wanted, *rest], start=1):
            self._conn.execute(
                "UPDATE claude_account SET position = ? WHERE slot = ?", (position, slot)
            )
        self._conn.commit()

    def promote_retired_claude_account_default(self) -> int | None:
        """Move the slot the retired ``is_default`` flag names to rank 1 and clear the flag.

        The machine default IS rank 1 of the order (#8 on the board), so a flag an
        older build set is folded into the order: that slot goes to the top, the
        others keep their order behind it, renumbered 1..n, and the flag is cleared
        — so a launch that took a signed-in, enabled old default takes the same account
        after the upgrade. Two do change. An old default that is signed out or disabled
        is skipped by the preferred rung, so the launch goes to the next enabled,
        signed-in account (before, a signed-out one opened on its login screen). And
        a role with a hand-written ``team bind <role> --env CLAUDE_CONFIG_DIR=…`` that
        ran on the old default or on a headroom pick now runs on its own directory,
        since a binding wins over both silent defaults (#9).
        Keyed on the DATA, not on a schema
        version: a store any line carried past whatever version a step would have had
        still holds the flag, and is promoted on its next read all the same. Clearing
        it makes this idempotent, and lets a later reorder stand. One transaction;
        the slot moved, or ``None`` when no flag was set. The column and its index
        stay (dropping a column is a table rebuild, and an older build reading this
        store still expects it).
        """
        row = self._conn.execute(
            "SELECT slot FROM claude_account WHERE is_default = 1 LIMIT 1"
        ).fetchone()
        if row is None:
            return None  # the common case: one read, no transaction, no lock
        promoted = int(row[0])
        # Clear the flag FIRST: the UPDATE takes the write lock, and only the one that
        # still finds the flag goes on. Reading the order before it, a promote racing a
        # promote-then-reorder wrote its stale order over the reorder (review of #406,
        # the lost update); a second promoter now finds no flag and writes nothing.
        cleared = self._conn.execute(
            "UPDATE claude_account SET is_default = 0 WHERE slot = ? AND is_default = 1",
            (promoted,),
        )
        if cleared.rowcount != 1:
            self._conn.rollback()
            return None
        current = [record.slot for record in self.claude_accounts()]  # under the lock
        order = [promoted, *(slot for slot in current if slot != promoted)]
        for position, slot in enumerate(order, start=1):
            self._conn.execute(
                "UPDATE claude_account SET position = ? WHERE slot = ?", (position, slot)
            )
        self._conn.commit()
        return promoted

    # --- usage readings (v16, #146) ------------------------------------------------------------

    def add_usage_sample(self, sample: UsageSample) -> None:
        """Record one reading and drop this slot's readings older than :data:`_USAGE_RETENTION`."""
        self._conn.execute(
            f"INSERT INTO claude_usage ({_USAGE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                sample.slot,
                sample.fetched_at.isoformat(),
                sample.session_percent,
                sample.session_resets_at.isoformat() if sample.session_resets_at else None,
                sample.week_percent,
                sample.week_resets_at.isoformat() if sample.week_resets_at else None,
                sample.token_fingerprint,
            ),
        )
        self._conn.execute(
            "DELETE FROM claude_usage WHERE slot = ? AND fetched_at < ?",
            (sample.slot, (sample.fetched_at - _USAGE_RETENTION).isoformat()),
        )
        self._conn.commit()

    def usage_samples(self, slot: int, *, since: datetime) -> list[UsageSample]:
        """This slot's readings taken at or after ``since``, oldest first."""
        rows = self._conn.execute(
            f"SELECT {_USAGE_COLUMNS} FROM claude_usage WHERE slot = ? AND fetched_at >= ? "
            "ORDER BY fetched_at",
            (slot, since.isoformat()),
        ).fetchall()
        return [_row_to_usage_sample(row) for row in rows]

    def delete_usage_samples(self, slot: int) -> int:
        """Drop every reading of ``slot``: a removed account's history must not rate the
        next occupant's window (review of #205, third round). Returns the count."""
        cursor = self._conn.execute("DELETE FROM claude_usage WHERE slot = ?", (slot,))
        self._conn.commit()
        return int(cursor.rowcount)

    # --- per-project settings (v15) ---------------------------------------------------------

    def project_setting(self, project_id: str, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM project_setting WHERE project_id = ? AND key = ?",
            (project_id, key),
        ).fetchone()
        return str(row[0]) if row is not None else None

    def set_project_setting(self, project_id: str, key: str, value: str) -> None:
        """Set one key for one project. The project row must exist (a foreign key)."""
        self._conn.execute(
            "INSERT INTO project_setting (project_id, key, value, set_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (project_id, key) DO UPDATE SET "
            "value = excluded.value, set_at = excluded.set_at",
            (project_id, key, value, _now_iso()),
        )
        self._conn.commit()

    def clear_project_setting(self, project_id: str, key: str) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM project_setting WHERE project_id = ? AND key = ?", (project_id, key)
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def project_settings(self, key: str) -> dict[str, str]:
        """``key``'s value for every project that has one, keyed by project id."""
        rows = self._conn.execute(
            "SELECT project_id, value FROM project_setting WHERE key = ? ORDER BY project_id",
            (key,),
        ).fetchall()
        return {str(row["project_id"]): str(row["value"]) for row in rows}

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

    def missing_schema(self) -> list[str]:
        """This build's tables, indexes, triggers and columns that this store lacks.

        The open has already converged what it can (:func:`_migrate`), so this is
        what it could not: an object no step from v15 on produces, missing from a
        store another build or a hand edit changed. Each is named for the operator,
        ``table claude_account``, ``column fleet_agent.account_slot``, ``unique index
        project_codename``, ``shadow table entry_fts_data`` or ``unreadable table
        entry_fts``. Empty for a store that holds this build's whole schema; tables and
        columns of another line's are not this build's and are never reported.
        """
        return _missing_from(self._conn, _ladder_schema())

    def has_table(self, table: str) -> bool:
        """Whether this store holds ``table``, as the open read its names: no query. A store
        without ``team_session`` or ``team_task`` (doctor's damaged-store case) still opens,
        and a read that only adds evidence asks this before it asks such a table."""
        return table in self._names

    def close(self) -> None:
        self._conn.close()


def _on_boards(project_ids: Collection[str] | None) -> tuple[str, tuple[str, ...]]:
    """The clause that keeps a lookup to the rows of ``project_ids``, and its values; none for
    ``None``. A lookup kept to them is ambiguous only among them: the remote's, whose phone
    may reach only some boards, was told by an ``ambiguous_id`` which prefixes another
    board's tasks and sessions had (review of #243, round 10, 5/7)."""
    if project_ids is None:
        return "", ()
    boards = tuple(project_ids)
    return f" AND project_id IN ({', '.join('?' * len(boards))})", boards


_GLOB_METACHARACTERS = str.maketrans("", "", "*?[")


def _glob_escape(text: str) -> str:
    """``text`` as a GLOB pattern that matches ``text`` itself: each ``*``, ``?`` and ``[`` in
    a class of its own (``[*]``), as GLOB has no escape character.

    It took them out, as a ref lookup does (:func:`_ref_stripped`), and so did not stand
    for itself: ``x[1`` was the prefix ``x1``, and a purge of the keys under an id holding
    one would have taken another id's (review of #243, round 11, 8/9)."""
    return re.sub(r"([*?\[])", r"[\1]", text)


def _ref_stripped(ref: str) -> str:
    """``ref`` with the GLOB metacharacters taken out, as an id lookup reads a ref: no id
    holds one, and ``ses_abc*`` is the prefix ``ses_abc``."""
    return ref.translate(_GLOB_METACHARACTERS)


def _glob_prefix(ref: str) -> str:
    """The GLOB pattern of an id that starts with ``ref``, its metacharacters taken out
    (:func:`_ref_stripped`); one that matches no id when nothing is left of it.

    ``*`` was then ``*``, which matched every id: a ref of ``*``, ``?`` or ``[`` named every
    project, task and session the store had, and a refusal of it as ambiguous listed them
    (review of #243, round 10, 7/7). An empty pattern matches only an empty id, which none is.
    """
    stripped = _ref_stripped(ref)
    return f"{stripped}*" if stripped else ""


def _event_values(event: TeamEvent) -> tuple[object, ...]:
    """``event``'s values for an ``INSERT INTO team_event`` of every written column."""
    return (
        event.id,
        event.project_id,
        event.session_id,
        event.kind,
        event.text,
        event.task_id,
        event.to_role,
        event.created_at.isoformat(),
        event.actor,
        None if event.meta is None else json.dumps(event.meta, sort_keys=True),
    )


_SESSION_META_KINDS: tuple[str, ...] = (
    "nudge", "unbriefed", "compact", "compact-refused", "compact-armed"
)  # fmt: skip
"""The ``team_meta`` keys kept per session as ``<kind>:<session id>``: the manager's nudge
debounce, the solo session's not-yet-briefed mark (#346), and the quiet compaction's mark,
its newest refusal and its latest arm (P1 WS-4)."""


class GroupSettings(NamedTuple):
    """A project group's parent manager settings (P2-W6, #199)."""

    group_id: str
    parent_project_id: str | None = None
    route: dict[str, object] | None = None
    """``{"kinds": [...], "pattern": str | None, "off": bool}``; ``None``: the defaults."""
    max_agents: int = 0
    """Live agents across the group's members and parent; ``0`` is no cap."""


class Relevance(NamedTuple):
    """What a ``relevant`` board context keeps (P1 WS-2, #279): events on these tasks,
    events addressed to these names (a role or a label, any case), and decisions unless
    ``decisions`` is off: what a ``none`` agent's turn end asks for, the notes addressed to
    it and nothing else (sync #365)."""

    task_ids: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    decisions: bool = True


def _relevant_clause(relevant: Relevance | None) -> tuple[str, list[str]]:
    """``AND (…) `` keeping only what ``relevant`` keeps, with its parameters; nothing for
    ``None``. In the query, before the LIMIT: forty irrelevant events must not fill a page
    the relevant one is behind (review of #240, finding 11's rule)."""
    if relevant is None:
        return "", []
    terms = ["kind = 'decision'"] if relevant.decisions else []
    params: list[str] = []
    if relevant.task_ids:
        terms.append(f"task_id IN ({', '.join('?' * len(relevant.task_ids))})")
        params += relevant.task_ids
    if relevant.names:
        terms.append(f"lower(to_role) IN ({', '.join('?' * len(relevant.names))})")
        params += [name.strip().lower() for name in relevant.names]
    if not terms:
        return "AND 0 ", []  # keeps nothing: no task, no name, no decisions
    return f"AND ({' OR '.join(terms)}) ", params


def _and_not_kinds(kinds: Collection[str]) -> str:
    """``AND kind NOT IN (?, …) `` for a query that leaves ``kinds`` out, or nothing; the
    caller hands ``*kinds`` to the query at that place."""
    return f"AND kind NOT IN ({', '.join('?' * len(kinds))}) " if kinds else ""


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


def _run_step(connection: sqlite3.Connection, version: int) -> None:
    """Step ``version`` → ``version + 1``, in the caller's transaction, without the bump.

    :data:`_PREPARE`, the script, then the step's columns from :data:`_PRODUCTS`,
    each followed by its :data:`_BACKFILLS` entry when it was added. This is also
    how :func:`_converge_by_presence` applies a step again, which every step
    from v15 on allows: each CREATE is ``IF NOT EXISTS``, each column is added
    only when absent, and v23's repair leaves a repaired row as it is.
    """
    prepare = _PREPARE.get(version)
    if prepare is not None:
        prepare(connection)
    for statement in _statements(_MIGRATIONS[version]):
        connection.execute(statement)
    for table, column, declaration in _PRODUCTS.get(version, _Products()).columns:
        if _add_column_if_absent(connection, table, column, declaration):
            backfill = _BACKFILLS.get((table, column))
            if backfill is not None:
                backfill(connection)


def _schema_names(connection: sqlite3.Connection) -> frozenset[str]:
    """Every name in ``sqlite_master``: the store's tables, indexes and triggers."""
    return frozenset(row[0] for row in connection.execute("SELECT name FROM sqlite_master"))


def _steps_missing(
    connection: sqlite3.Connection, below: int, names: frozenset[str] | None = None
) -> list[int]:
    """The steps under ``below``, from v15 on, that left something out of this store.

    A store stamped ``below`` claims to have run them all; one that another line
    stamped has run that line's steps under these numbers instead. Only reads:
    one ``sqlite_master`` query, unless the caller read ``names`` already, and a
    ``table_info`` per table a column belongs to.
    """
    steps = sorted(step for step in _PRODUCTS if step < below)
    if not steps:
        return []
    if names is None:
        names = _schema_names(connection)
    tables = {table for step in steps for table, _, _ in _PRODUCTS[step].columns}
    columns = {
        table: {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        for table in tables
    }
    return [
        step
        for step in steps
        if not names.issuperset(_PRODUCTS[step].objects)
        or any(column not in columns[table] for table, column, _ in _PRODUCTS[step].columns)
    ]


def _converge_by_presence(connection: sqlite3.Connection, below: int) -> None:
    """Apply again each step under ``below`` whose products this store does not hold.

    In the caller's transaction, which holds the write lock, so a racing opener
    finds the products in place and does nothing. A store that holds them pays
    the reads in :func:`_steps_missing` and nothing more. A column added here
    brings its backfill, so a store that skipped v17 lists its projects.
    """
    for step in _steps_missing(connection, below):
        _run_step(connection, step)


def _migrate(connection: sqlite3.Connection) -> frozenset[str]:
    """Bring the schema to the current version, safely under concurrency; the names in
    ``sqlite_master`` once it is done (:func:`_schema_names`).

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

    THE VERSION ALONE IS NOT TRUSTED FROM v15 ON: other lines of development
    stamped 15-17 for steps of their own (the note above _SCHEMA_V15), so a
    store may claim steps it never ran. Before each step, and once more after
    the last, :func:`_converge_by_presence` applies again any step below the
    version whose tables, indexes or columns the store lacks. Before each step,
    because a later step can need an earlier one's products: v23 updates
    ``onboarded_at``, which a store stamped 17 by #113 does not have. After the
    last, because a store stamped current never enters the loop, including one
    that a build without this pass carried to current past a step it skipped.
    Then :func:`_number_unnumbered_tasks` numbers any task an older build added.

    The names the last presence pass read are what it returns, read again only when it
    applied a step: the store asks them of nothing else (:class:`SqliteStore`). Asked again by
    each store, at its first fleet read, whether it had ``team_session``, every
    ``store_session()`` that read a fleet row paid a ``sqlite_master`` query of its own, and
    hooks and Remote's 3 s needs scan open many (review of #259, round 28, 9/10).
    """
    while True:
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) >= len(_MIGRATIONS):
            break
        try:
            connection.execute("BEGIN IMMEDIATE")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version >= len(_MIGRATIONS):
                connection.execute("COMMIT")
                break
            _converge_by_presence(connection, version)
            _run_step(connection, version)
            connection.execute(f"PRAGMA user_version = {version + 1}")
            connection.execute("COMMIT")
        except sqlite3.Error:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            raise
    names = _schema_names(connection)
    if _steps_missing(connection, len(_MIGRATIONS), names):
        try:
            connection.execute("BEGIN IMMEDIATE")
            _converge_by_presence(connection, len(_MIGRATIONS))
            connection.execute("COMMIT")
        except sqlite3.Error:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            raise
        names = _schema_names(connection)
    _number_unnumbered_tasks(connection)
    return names


#: The suffixes of the shadow tables FTS5 keeps a table's index in, ``entry_fts_data``
#: and so on (sqlite.org/fts5.html). Which of them a table has depends on its options:
#: ``entry_fts`` reads its text from ``entry`` and has no ``_content``.
_FTS5_SHADOWS = ("data", "idx", "content", "docsize", "config")


class _Schema(NamedTuple):
    """A database's schema by name, as :func:`_missing_from` compares it."""

    tables: dict[str, frozenset[str]]
    """Each table, ordinary, virtual or shadow, with its columns."""
    objects: dict[str, tuple[str, str]]
    """Each index and trigger: its kind (``index``, ``unique index`` or ``trigger``) and
    the table it belongs to."""
    shadows: dict[str, str]
    """Each FTS5 shadow table among :attr:`tables` and the virtual table it belongs to."""


def _ladder_schema() -> _Schema:
    """What the ladder builds on an empty database: this build's whole schema.

    Built, not listed, so it cannot drift from the steps: every step from v1 on,
    run by :func:`_migrate` itself in memory (a few milliseconds). SQLite's own
    objects (``sqlite_sequence``, automatic indexes) come and go with the tables
    that cause them and are left out. FTS5's shadow tables are the module's own
    storage, made and dropped with their virtual table, and are kept with it: a
    store without ``entry_fts`` lacks one table, not five, and one that lost only
    ``entry_fts_data`` lacks that, without which no note can be added. Told apart
    by FTS5's suffixes, not by the virtual table's name alone, so a table of this
    build's named ``entry_fts_…`` is not taken for one.
    """
    connection = sqlite3.connect(":memory:")
    try:
        _migrate(connection)
        tables: dict[str, frozenset[str]] = {}
        objects: dict[str, tuple[str, str]] = {}
        shadows = {
            f"{owner}_{suffix}": owner
            for (owner,) in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND sql LIKE 'CREATE VIRTUAL TABLE %USING fts5%'"
            ).fetchall()
            for suffix in _FTS5_SHADOWS
        }
        for kind, name, table in connection.execute(
            "SELECT type, name, tbl_name FROM sqlite_master "
            "WHERE name NOT GLOB 'sqlite_*' ORDER BY rowid"
        ).fetchall():
            if kind == "table":
                columns = connection.execute(f"PRAGMA table_info({name})").fetchall()
                tables[name] = frozenset(column[1] for column in columns)
            elif kind == "index":
                # Told apart because their absence costs different things: a missing
                # unique index lets in the duplicates it refused, any other only slows
                # the reads it served, and doctor's database row says which.
                listed = connection.execute(f"PRAGMA index_list({table})").fetchall()
                unique = any(row[1] == name and row[2] for row in listed)
                objects[name] = ("unique index" if unique else kind, table)
            elif kind == "trigger":
                objects[name] = (kind, table)
        return _Schema(
            tables, objects, {name: owner for name, owner in shadows.items() if name in tables}
        )
    finally:
        connection.close()


def _missing_from(connection: sqlite3.Connection, expected: _Schema) -> list[str]:
    """``expected``'s tables, columns, indexes and triggers that ``connection`` lacks.

    Tables first, in the order the ladder made them, each followed by its missing
    columns by name; then indexes and triggers, again in the ladder's order. A
    missing table is named once, not with each of its columns and indexes, and a
    missing FTS5 table not with its shadow tables. A shadow table missing beside
    its FTS5 table is named ``shadow table …``, since it costs something of its
    own: the index can be neither written nor searched. An FTS5 table the module
    cannot open though its shadow tables are all there is named ``unreadable table
    …``, at the same cost. Only reads, and only this build's tables: another line's
    are not looked at.
    """
    present = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    missing: list[str] = []
    for table, columns in expected.tables.items():
        owner = expected.shadows.get(table)
        if owner is not None:
            # Its layout is the module's, so only its presence is compared.
            if owner in present and table not in present:
                missing.append(f"shadow table {table}")
            continue
        if table not in present:
            missing.append(f"table {table}")
            continue
        if any(shadow not in present for shadow, of in expected.shadows.items() if of == table):
            # Listing an FTS5 table's columns opens it, and the module reads its
            # `_config` shadow to do that: without it this read raised "vtable
            # constructor failed", which doctor's row took for an unreadable store.
            continue
        try:
            found = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        except sqlite3.DatabaseError as exc:
            if table not in expected.shadows.values() or is_locked_error(exc):
                raise
            # Every shadow table is there and the module still cannot open it: its
            # `_config` lost its version row, or holds a version this SQLite does not
            # read, and FTS5 answers "invalid fts5 file format". It is the index that
            # is unreadable, not the store, whose notes all read, so the row names it
            # instead of calling the store unreadable and offering the move.
            missing.append(f"unreadable table {table}")
            continue
        missing += [f"column {table}.{column}" for column in sorted(columns - found)]
    missing += [
        f"{kind} {name}"
        for name, (kind, table) in expected.objects.items()
        if name not in present and table in present
    ]
    return missing


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
            names = _migrate(connection)
            _remove_old_packs()
            return SqliteStore(connection, names=names)
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
