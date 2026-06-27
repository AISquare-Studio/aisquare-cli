"""The context store: remembered facts in the user and project pools."""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from aisquare.core.config import load_config
from aisquare.core.editor import edit_text
from aisquare.core.ids import new_entry_id
from aisquare.core.injection import build_block, record_injection
from aisquare.core.store import store_session
from aisquare.core.workspace import current_project
from aisquare.models import ContextEntry, ExportFormat, Pool


def remember(text: str, pool: Pool | None, tags: list[str]) -> ContextEntry:
    """Store ``text`` as a context entry (shorthand for ``context add``)."""
    return add_entry(text, pool, tags)


def inject() -> str:
    """Assemble the in-scope context block, record the injection, and return it."""
    with store_session() as store:
        project = current_project()
        entries = store.entries(project_id=project.id)
    record_injection(entries, project)
    return build_block(entries, project)


def list_entries() -> list[ContextEntry]:
    """List context in scope here: the user pool plus the current project's."""
    with store_session() as store:
        return store.entries(project_id=current_project().id)


def _build_entry(
    text: str, pool: Pool, project_id: str | None, tags: list[str], source: str
) -> ContextEntry:
    now = datetime.now(tz=UTC)
    return ContextEntry(
        id=new_entry_id(),
        pool=pool,
        project_id=project_id,
        text=text,
        tags=tags,
        source=source,
        created_at=now,
        updated_at=now,
    )


def add_entry(text: str, pool: Pool | None, tags: list[str]) -> ContextEntry:
    """Add a context entry to the user or project pool."""
    resolved: Pool = pool or load_config().default_pool
    with store_session() as store:
        project_id: str | None = None
        if resolved == "project":
            project = current_project()
            store.ensure_project(project)
            project_id = project.id
        return store.add(_build_entry(text, resolved, project_id, tags, "cli"))


def show_entry(entry_id: str) -> ContextEntry:
    """Show one context entry in full.

    Raises ``KeyError`` if nothing matches and ``AmbiguousIdError`` if the id
    prefix matches more than one entry.
    """
    with store_session() as store:
        entry = store.get(entry_id)
        if entry is None:
            raise KeyError(entry_id)
        return entry


def edit_entry(entry_id: str) -> ContextEntry:
    """Open a context entry's text in the user's editor and save any changes."""
    with store_session() as store:
        entry = store.get(entry_id)
        if entry is None:
            raise KeyError(entry_id)
        edited = edit_text(entry.text)
        if edited is None or edited == entry.text:
            return entry
        return store.update(entry.id, text=edited)


def remove_entry(entry_id: str) -> None:
    """Delete a context entry."""
    with store_session() as store:
        store.delete(entry_id)


def search_entries(query: str) -> list[ContextEntry]:
    """Search context in scope here: the user pool plus the current project's."""
    with store_session() as store:
        return store.search(query, project_id=current_project().id)


def preview() -> str:
    """Return the context block that would be injected right now (no side effects)."""
    with store_session() as store:
        project = current_project()
        entries = store.entries(project_id=project.id)
    return build_block(entries, project)


def import_entries(file: Path) -> int:
    """Import entries from a Markdown or JSON file; return the number added.

    JSON files are read as an array of objects with a ``text`` field (and
    optional ``tags`` and ``pool``); any other file is parsed as Markdown, one
    list item per entry. Imported entries get fresh ids and project-pool ones
    are scoped to the current project, so an export from one machine imports
    cleanly on another. Raises ``FileNotFoundError`` or ``ValueError`` on bad
    input.
    """
    incoming = _parse_import(file)
    default_pool = load_config().default_pool
    with store_session() as store:
        project = current_project()
        project_ready = False
        count = 0
        for item in incoming:
            pool = item.pool or default_pool
            project_id: str | None = None
            if pool == "project":
                if not project_ready:
                    store.ensure_project(project)
                    project_ready = True
                project_id = project.id
            store.add(_build_entry(item.text, pool, project_id, item.tags, "import"))
            count += 1
        return count


def export_entries(file: Path | None, fmt: ExportFormat) -> None:
    """Export in-scope entries to ``file`` (or stdout) as Markdown or JSON."""
    with store_session() as store:
        entries = store.entries(project_id=current_project().id)
    content = _to_json(entries) if fmt is ExportFormat.json else _to_markdown(entries)
    if file is None:
        sys.stdout.write(content)
    else:
        file.write_text(content, encoding="utf-8")


def promote_entry(entry_id: str) -> ContextEntry:
    """Promote a project-pool entry into the user pool.

    Raises ``ValueError`` if the entry is already in the user pool.
    """
    with store_session() as store:
        return store.promote(entry_id)


# --- import/export serialization -------------------------------------------


class _Incoming(NamedTuple):
    """An entry parsed from an import file, before it is stored."""

    text: str
    tags: list[str]
    pool: Pool | None


_BULLET = re.compile(r"^\s*[-*]\s+(.*\S)\s*$")
_HASHTAG = re.compile(r"\s+#([A-Za-z0-9_-]+)")


def _parse_import(file: Path) -> list[_Incoming]:
    content = file.read_text(encoding="utf-8")
    if file.suffix.lower() == ".json":
        return _parse_json(content)
    return _parse_markdown(content)


def _parse_json(content: str) -> list[_Incoming]:
    data = json.loads(content)
    if not isinstance(data, list):
        raise ValueError("expected a JSON array of entries")
    items: list[_Incoming] = []
    for obj in data:
        if not isinstance(obj, dict) or "text" not in obj:
            raise ValueError("each entry needs a 'text' field")
        raw_pool = obj.get("pool")
        pool: Pool | None = raw_pool if raw_pool in ("user", "project") else None
        tags = [str(tag) for tag in obj.get("tags", [])]
        items.append(_Incoming(text=str(obj["text"]), tags=tags, pool=pool))
    return items


def _parse_markdown(content: str) -> list[_Incoming]:
    items: list[_Incoming] = []
    for line in content.splitlines():
        match = _BULLET.match(line)
        if match is None:
            continue
        text = match.group(1)
        tags = _HASHTAG.findall(text)
        if tags:
            text = _HASHTAG.sub("", text).strip()
        items.append(_Incoming(text=text, tags=tags, pool=None))
    return items


def _to_json(entries: list[ContextEntry]) -> str:
    return json.dumps([entry.model_dump(mode="json") for entry in entries], indent=2) + "\n"


def _to_markdown(entries: list[ContextEntry]) -> str:
    lines = ["# aisquare context", ""]
    for pool, heading in (("user", "## User"), ("project", "## Project")):
        in_pool = [entry for entry in entries if entry.pool == pool]
        if not in_pool:
            continue
        lines.append(heading)
        lines.append("")
        for entry in in_pool:
            suffix = "".join(f" #{tag}" for tag in entry.tags)
            lines.append(f"- {entry.text}{suffix}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"
