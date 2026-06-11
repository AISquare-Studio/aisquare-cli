"""``aisquare context`` (alias ``ctx``) — manage stored context entries."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from aisquare.cli.common import resolve_pool
from aisquare.models import ExportFormat
from aisquare.services import context as context_service

app = typer.Typer(help="Inspect and edit remembered context (alias: ctx).", no_args_is_help=True)

EntryId = Annotated[str, typer.Argument(help="Context entry id.")]


@app.command("list")
def list_() -> None:
    """List stored context entries."""
    context_service.list_entries()


@app.command("add")
def add(
    text: Annotated[str, typer.Argument(help="The fact or convention to store.")],
    user: Annotated[bool, typer.Option("--user", help="Store in the user (global) pool.")] = False,
    project: Annotated[
        bool, typer.Option("--project", help="Store in the current project pool.")
    ] = False,
    tag: Annotated[
        list[str] | None,
        typer.Option("--tag", help="Tag for the entry; repeat for several."),
    ] = None,
) -> None:
    """Add a context entry."""
    context_service.add_entry(text, pool=resolve_pool(user, project), tags=tag or [])


@app.command("show")
def show(entry_id: EntryId) -> None:
    """Show a context entry in full."""
    context_service.show_entry(entry_id)


@app.command("edit")
def edit(entry_id: EntryId) -> None:
    """Edit a context entry in your editor."""
    context_service.edit_entry(entry_id)


@app.command("remove")
def remove(entry_id: EntryId) -> None:
    """Delete a context entry."""
    context_service.remove_entry(entry_id)


@app.command("search")
def search(query: Annotated[str, typer.Argument(help="Search text.")]) -> None:
    """Search context entries by text and tags."""
    context_service.search_entries(query)


@app.command("preview")
def preview() -> None:
    """Preview what would be injected into an agent session right now."""
    context_service.preview()


@app.command("import")
def import_(
    file: Annotated[Path, typer.Argument(help="Markdown or JSON file to import.")],
) -> None:
    """Import context entries from a file."""
    context_service.import_entries(file)


@app.command("export")
def export(
    file: Annotated[Path | None, typer.Argument(help="Destination file (default: stdout).")] = None,
    format_: Annotated[
        ExportFormat, typer.Option("--format", help="Output format.")
    ] = ExportFormat.md,
) -> None:
    """Export context entries to Markdown or JSON."""
    context_service.export_entries(file, fmt=format_)


@app.command("promote")
def promote(entry_id: EntryId) -> None:
    """Promote a project entry into the user pool."""
    context_service.promote_entry(entry_id)
