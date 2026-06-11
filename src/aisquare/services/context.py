"""The context store: remembered facts in the user and project pools."""

from __future__ import annotations

from pathlib import Path

from aisquare.core.stubs import stub
from aisquare.models import ContextEntry, ExportFormat, Pool


def remember(text: str, pool: Pool | None, tags: list[str]) -> ContextEntry:
    """Store ``text`` as a context entry (shorthand for ``context add``)."""
    stub("remember")


def inject() -> None:
    """Inject relevant context into the current agent session."""
    stub("inject")


def list_entries() -> list[ContextEntry]:
    """List stored context entries."""
    stub("context list")


def add_entry(text: str, pool: Pool | None, tags: list[str]) -> ContextEntry:
    """Add a context entry to the user or project pool."""
    stub("context add")


def show_entry(entry_id: str) -> ContextEntry:
    """Show one context entry in full."""
    stub("context show")


def edit_entry(entry_id: str) -> ContextEntry:
    """Open a context entry in the user's editor."""
    stub("context edit")


def remove_entry(entry_id: str) -> None:
    """Delete a context entry."""
    stub("context remove")


def search_entries(query: str) -> list[ContextEntry]:
    """Search context entries by text and tags."""
    stub("context search")


def preview() -> None:
    """Preview the context that would be injected right now."""
    stub("context preview")


def import_entries(file: Path) -> int:
    """Import entries from a Markdown or JSON file; return the count."""
    stub("context import")


def export_entries(file: Path | None, fmt: ExportFormat) -> None:
    """Export entries to ``file`` (or stdout) in the given format."""
    stub("context export")


def promote_entry(entry_id: str) -> ContextEntry:
    """Promote a project-pool entry into the user pool."""
    stub("context promote")
