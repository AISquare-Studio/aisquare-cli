"""Domain models shared by the CLI and service layers.

Storage is not implemented yet, but these models pin down the shapes the
services will accept and return, so the CLI layer can be wired against them
from day one.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

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


class DoctorCheck(BaseModel):
    """One diagnostic check result from ``doctor``."""

    name: str
    ok: bool
    detail: str


class PromptRecord(BaseModel):
    """A captured user prompt — how the user asked their agent, for replay."""

    id: str
    project_id: str | None = None
    text: str
    source: str = "claude-code"
    created_at: datetime


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
