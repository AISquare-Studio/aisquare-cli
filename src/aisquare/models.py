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
    text: str
    tags: list[str] = Field(default_factory=list)
    source: str = "manual"
    created_at: datetime


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
