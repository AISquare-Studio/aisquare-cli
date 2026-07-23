"""Domain models shared by the CLI and service layers.

Storage is not implemented yet, but these models pin down the shapes the
services will accept and return, so the CLI layer can be wired against them
from day one.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

_SIGNAL_TEXT = re.compile(r"^([a-z0-9][a-z0-9._-]*): (\S+)(?: \(was (\S+)\))?$")
"""The anchored serialization of a signal event's text: ``name: value`` with
an optional `` (was prev)`` tail. Names and values are validated single
tokens at set time, so the decode is exact — never substring matching."""

Pool = Literal["user", "project"]
"""Where context lives: the global user pool or the current project pool."""


class ExportFormat(StrEnum):
    """Supported ``context export`` output formats."""

    md = "md"
    json = "json"


class RedactionLevel(StrEnum):
    """How aggressively captured data is scrubbed before storage."""

    off = "off"
    standard = "standard"
    strict = "strict"


class ContextEntry(BaseModel):
    """A single remembered fact, preference, or convention."""

    id: str
    pool: Pool
    project_id: str | None = None
    """Owning project for ``pool == "project"`` entries; ``None`` for the user pool."""
    text: str
    tags: list[str] = Field(default_factory=list)
    source: str = "manual"
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
    """Set when the entry is soft-deleted; tombstones survive so deletes can sync."""


class DataEnvelope(BaseModel):
    """Payload exchanged over the capture pipe between agents and aisquare."""

    kind: str
    scope: Pool
    payload: dict[str, Any]
    source: str
    ts: datetime


class AgentInfo(BaseModel):
    """A coding agent aisquare knows how to integrate with."""

    name: str
    detected: bool = False
    config_paths: list[Path] = Field(default_factory=list)
    connected: bool = False


class ProjectInfo(BaseModel):
    """A project (workspace) tracked by aisquare."""

    id: str
    root: Path
    linked_repos: list[str] = Field(default_factory=list)


class InjectionRecord(BaseModel):
    """A record of the most recent context injection, surfaced by ``why``."""

    injected_at: datetime
    project_id: str | None = None
    user_count: int = 0
    project_count: int = 0
    entry_ids: list[str] = Field(default_factory=list)


class SetupReport(BaseModel):
    """What ``init`` did, for reporting back to the user."""

    home: Path
    already_initialized: bool
    project: ProjectInfo
    onboarded: int = 0
    notes: list[str] = Field(default_factory=list)


class StatusReport(BaseModel):
    """A snapshot of installation health for ``status``."""

    home: Path
    initialized: bool
    user_entries: int
    project_entries: int
    active_project: ProjectInfo
    project_count: int
    agents_detected: list[str] = Field(default_factory=list)
    agents_connected: list[str] = Field(default_factory=list)


class CheckStatus(StrEnum):
    """Severity of a ``doctor`` check: healthy, degraded-but-usable, or broken."""

    ok = "ok"
    warn = "warn"
    fail = "fail"


class DoctorCheck(BaseModel):
    """One diagnostic check result from ``doctor``."""

    name: str
    status: CheckStatus
    detail: str
    fix: str | None = None  # actionable guidance when not ok


class PromptRecord(BaseModel):
    """A captured user prompt — how the user asked their agent, for replay."""

    id: str
    project_id: str | None = None
    text: str
    source: str = "claude-code"
    created_at: datetime


TaskStatus = Literal["todo", "doing", "review", "blocked", "done", "dropped"]
"""Lifecycle of a shared team task: todo → doing → review → done (or parked)."""


class TeamSession(BaseModel):
    """One live agent session on the orchestrator (id = the agent's session id)."""

    id: str
    project_id: str
    role: str = "unassigned"
    label: str | None = None
    focus: str | None = None
    """What this session says it is working on right now."""
    started_at: datetime
    last_seen_at: datetime
    ended_at: datetime | None = None
    cursor: int = 0
    """Highest team-event ``seq`` already shown to this session (delta position)."""
    state: str = "working"
    """Live activity: working (mid-turn), waiting (wants input) or attention."""
    transcript_path: str | None = None
    """The session's Claude Code transcript (JSONL), from hook payloads."""


class TeamTask(BaseModel):
    """A shared task on the orchestrator, idempotent on ``(project_id, key)``."""

    id: str
    project_id: str
    key: str
    """Idempotency key: re-adding the same key returns the existing task."""
    title: str
    detail: str | None = None
    status: TaskStatus = "todo"
    role: str | None = None
    """Suggested owner role (planner/coder/runner/...), advisory only."""
    needs: list[str] = Field(default_factory=list)
    """Task ids this one depends on; ``task next`` only hands out ready tasks."""
    claimed_by: str | None = None
    claim_expires_at: datetime | None = None
    """Claim lease; an expired lease makes the task claimable again."""
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime


class TeamEvent(BaseModel):
    """One update on the team pipe: who did what, addressed to whom.

    ``seq`` is the gapless stream cursor (SQLite rowid); deltas are "events with
    ``seq`` past my cursor, authored by others". Events render as
    :class:`DataEnvelope` payloads so the team stream stays pipe-shaped.
    """

    seq: int = 0
    id: str
    project_id: str
    session_id: str | None = None
    kind: str = "note"
    text: str
    task_id: str | None = None
    to_role: str | None = None
    created_at: datetime

    def as_envelope(self) -> DataEnvelope:
        """This event as a capture-pipe envelope (``kind`` namespaced ``team.*``).

        ``signal`` events additionally carry structured ``name``/``value``/
        ``prev``/``set_by`` payload fields (#23) so consumers key on fields,
        never on free text — decoded from the event's own anchored text
        format, which token validation at set time makes unambiguous.
        """
        payload: dict[str, object | None] = {
            "seq": self.seq,
            "id": self.id,
            "project_id": self.project_id,
            "session_id": self.session_id,
            "text": self.text,
            "task_id": self.task_id,
            "to_role": self.to_role,
        }
        if self.kind == "signal":
            decoded = _SIGNAL_TEXT.match(self.text)
            if decoded:
                payload.update(
                    {
                        "name": decoded.group(1),
                        "value": decoded.group(2),
                        "prev": decoded.group(3),
                        "set_by": self.session_id,
                    }
                )
        return DataEnvelope(
            kind=f"team.{self.kind}",
            scope="project",
            payload=payload,
            source=self.session_id or "cli",
            ts=self.created_at,
        )


class Snapshot(BaseModel):
    """A packed snapshot of a project's codebase (Repomix full pack + skeleton)."""

    project_id: str
    head_sha: str | None = None
    generated_at: datetime
    pack_path: Path
    skeleton_path: Path
    index_path: Path
    token_count: int = 0
    skeleton_token_count: int = 0
    file_count: int = 0
    compressed: bool = False
    status: str = "ready"


class OnboardReport(BaseModel):
    """Outcome of ``project onboard``: seeded facts and the codebase snapshot."""

    seeded: list[ContextEntry] = Field(default_factory=list)
    snapshot: Snapshot | None = None


class AgentConnection(BaseModel):
    """Outcome of ``agents connect``: hook install + context ingested."""

    name: str
    hooks_installed: bool = False
    imported: int = 0
