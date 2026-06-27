"""Construct fresh context entries.

A tiny shared factory so every producer of entries (manual ``add``, ``import``,
project ``onboard``) mints ids and timestamps the same way.
"""

from __future__ import annotations

from datetime import UTC, datetime

from aisquare.core.ids import new_entry_id
from aisquare.models import ContextEntry, Pool


def new_entry(
    text: str,
    pool: Pool,
    project_id: str | None,
    tags: list[str],
    source: str,
) -> ContextEntry:
    """Build a new :class:`ContextEntry` with a fresh id and current timestamps."""
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
