"""Import Claude Code's auto-memory files into aisquare's pools (Tier 1).

Claude Code keeps a per-directory memory: one Markdown file per fact under
``~/.claude/projects/<slug>/memory/``, with typed frontmatter. Those files are
the only reason a fresh agent session knows anything — and they are loaded per
*launch directory*, so a fact saved from ``$HOME`` never reaches a session
started inside a sub-repository. aisquare's pools do reach every session, so
this importer moves the facts across.

Mapping: ``type: user`` and ``type: feedback`` are about the person and how to
work with them — they go to the user pool. ``type: project`` and ``type:
reference`` describe a body of work; the memory directory's slug cannot be
reliably inverted to a path (the encoding is lossy), so the caller says where
they belong: ``--stream NAME`` sends them to that stream's pool, and without it
they land in the user pool tagged with their type so they can be re-homed
later.

Idempotent: every import is tagged ``claude-memory:<file name>``; a fact whose
tag already exists is skipped, so re-running after adding one new memory file
imports exactly that one.

No model call anywhere in this module — the files are already curated.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from aisquare.core.entries import new_entry
from aisquare.core.store import ContextStore, store_session
from aisquare.models import ContextEntry
from aisquare.services import stream as stream_service

MEMORY_TYPES = ("user", "feedback", "project", "reference")
_TAG_PREFIX = "claude-memory:"
_INDEX_NAME = "MEMORY.md"


class MemoryFile(BaseModel):
    """One parsed auto-memory file."""

    path: Path
    name: str
    description: str = ""
    type: str = "project"
    body: str = ""


class ImportReport(BaseModel):
    """What ``import claude-memory`` did."""

    scanned: int = 0
    imported: list[ContextEntry] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    """Names already present (their ``claude-memory:`` tag exists)."""


def default_claude_projects() -> Path:
    """Where Claude Code keeps per-directory session data and memory."""
    return Path.home() / ".claude" / "projects"


def import_memory(claude_projects: Path, *, stream: str | None = None) -> ImportReport:
    """Import every auto-memory file under ``claude_projects`` into the pools."""
    files = scan_memory(claude_projects)
    report = ImportReport(scanned=len(files))
    with store_session() as store:
        target = stream_service.resolve(store, stream) if stream is not None else None
        existing = _existing_tags(store, target.id if target else None)
        for memory in files:
            tag = _TAG_PREFIX + memory.name
            if tag in existing:
                report.skipped.append(memory.name)
                continue
            text = _entry_text(memory)
            tags = [tag, f"type:{memory.type}"]
            if memory.type in ("user", "feedback") or target is None:
                entry = new_entry(text, "user", None, tags, "claude-memory")
            else:
                entry = new_entry(text, "stream", None, tags, "claude-memory", stream_id=target.id)
            report.imported.append(store.add(entry))
            existing.add(tag)
    return report


def scan_memory(claude_projects: Path) -> list[MemoryFile]:
    """Parse every memory file under ``claude_projects`` (index files skipped).

    The index (``MEMORY.md``) is one line per memory — importing it would
    duplicate every fact as a stub.
    """
    if not claude_projects.is_dir():
        raise FileNotFoundError(f"no Claude Code projects directory at {claude_projects}")
    files: list[MemoryFile] = []
    for path in sorted(claude_projects.glob("*/memory/*.md")):
        if path.name == _INDEX_NAME:
            continue
        files.append(_parse(path))
    return files


def _entry_text(memory: MemoryFile) -> str:
    head = memory.description or memory.name
    body = memory.body.strip()
    return f"{head}\n{body}" if body else head


def _existing_tags(store: ContextStore, stream_id: str | None) -> set[str]:
    """Every ``claude-memory:`` tag already present in the pools we write to."""
    entries = store.entries("user")
    if stream_id is not None:
        entries += store.entries("stream", stream_ids=[stream_id])
    return {tag for entry in entries for tag in entry.tags if tag.startswith(_TAG_PREFIX)}


def _parse(path: Path) -> MemoryFile:
    """Parse one memory file's frontmatter and body — tolerantly, no YAML dep.

    The frontmatter is machine-written and shallow (``name``, ``description``,
    ``metadata.type``); anything unparseable degrades to defaults rather than
    failing the whole import over one odd file.
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    name = path.stem
    description = ""
    type_ = "project"
    body = raw
    lines = raw.splitlines()
    if lines and lines[0].strip() == "---":
        for closing, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                body = "\n".join(lines[closing + 1 :])
                for front in lines[1:closing]:
                    stripped = front.strip()
                    if stripped.startswith("name:"):
                        name = _unquote(stripped.removeprefix("name:")) or name
                    elif stripped.startswith("description:"):
                        description = _unquote(stripped.removeprefix("description:"))
                    elif stripped.startswith("type:"):
                        candidate = _unquote(stripped.removeprefix("type:"))
                        if candidate in MEMORY_TYPES:
                            type_ = candidate
                break
    return MemoryFile(path=path, name=name, description=description, type=type_, body=body)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value.strip()
