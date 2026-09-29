-- user_version 21
-- a fresh store built by rc/captain-v1 a880966e's ladder, dumped 2026-09-29 from a copy in docker: sqlite_master's SQL in
-- rowid order, without SQLite's own objects or FTS5's shadow tables (made by
-- CREATE VIRTUAL TABLE). Frozen: a pin replays it, never this build's ladder.
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
, codename TEXT, forgotten_at TEXT, onboarded_at TEXT, group_id TEXT REFERENCES project_group (id), position INTEGER, pinned_at TEXT);
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
CREATE TABLE prompt (
    id          TEXT PRIMARY KEY,
    project_id  TEXT REFERENCES project (id),
    text        TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'claude-code',
    created_at  TEXT NOT NULL
);
CREATE INDEX prompt_project ON prompt (project_id, created_at);
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
, state TEXT NOT NULL DEFAULT 'working', transcript_path TEXT, account TEXT, model TEXT, effort TEXT, persona TEXT, limit_resets_at TEXT);
CREATE INDEX team_session_project ON team_session (project_id, last_seen_at);
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
    updated_at        TEXT NOT NULL, needs TEXT NOT NULL DEFAULT '[]',
    UNIQUE (project_id, key)
);
CREATE INDEX team_task_project_status ON team_task (project_id, status);
CREATE TABLE team_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
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
, account_slot INTEGER, persona TEXT, launch_spec TEXT);
CREATE INDEX fleet_agent_project ON fleet_agent (project_id, created_at);
CREATE UNIQUE INDEX fleet_agent_live_label ON fleet_agent (project_id, label)
    WHERE ended_at IS NULL;
CREATE UNIQUE INDEX project_codename ON project (codename);
CREATE TABLE metric (

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
CREATE INDEX metric_project_started ON metric (project_id, started_at);
CREATE INDEX metric_open_session ON metric (session_id, started_at)
    WHERE ended_at IS NULL;
CREATE TABLE claude_account (
    slot        INTEGER PRIMARY KEY,
    config_dir  TEXT NOT NULL,
    alias       TEXT,
    position    INTEGER NOT NULL,
    is_default  INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    disabled    INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)),
    created_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX claude_account_alias ON claude_account (alias) WHERE alias IS NOT NULL;
CREATE UNIQUE INDEX claude_account_default ON claude_account (is_default) WHERE is_default = 1;
CREATE TABLE project_setting (
    project_id  TEXT NOT NULL REFERENCES project (id),
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    set_at      TEXT NOT NULL,
    PRIMARY KEY (project_id, key)
);
CREATE TABLE claude_usage (
    slot               INTEGER NOT NULL,
    fetched_at         TEXT NOT NULL,
    session_percent    REAL,
    session_resets_at  TEXT,
    week_percent       REAL,
    week_resets_at     TEXT
);
CREATE INDEX claude_usage_slot_time ON claude_usage (slot, fetched_at);
CREATE TABLE ui_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE project_explainability (
    project_id TEXT PRIMARY KEY REFERENCES project (id),
    target     TEXT NOT NULL,
    key_path   TEXT NOT NULL,
    set_at     TEXT NOT NULL,
    set_by     TEXT
);
CREATE TABLE project_group (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    position   INTEGER NOT NULL DEFAULT 0,
    pinned_at  TEXT,
    collapsed  INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE project_destination (
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
