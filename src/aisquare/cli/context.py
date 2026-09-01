"""``aisquare context`` (alias ``ctx``) — manage stored context entries."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, NoReturn

import typer

from aisquare.cli.common import (
    emit_block,
    emit_entries,
    emit_entry,
    emit_entry_detail,
    emit_exported,
    emit_imported,
    emit_removed,
    fail,
    resolve_pool,
)
from aisquare.core.store import AmbiguousIdError
from aisquare.models import ExportFormat
from aisquare.services import context as context_service

app = typer.Typer(help="Inspect and edit remembered context (alias: ctx).", no_args_is_help=True)

EntryId = Annotated[str, typer.Argument(help="Context entry id.")]


def _fail_lookup(ref: str, exc: Exception) -> NoReturn:
    """Translate a store lookup failure into a clean CLI error."""
    if isinstance(exc, AmbiguousIdError):
        fail(f"id '{ref}' is ambiguous — use more characters", error="ambiguous_id", ref=ref)
    fail(f"no context entry matches '{ref}'", error="not_found", ref=ref)


@app.command("list")
def list_() -> None:
    """List stored context entries."""
    emit_entries(context_service.list_entries())


@app.command("add")
def add(
    text: Annotated[str, typer.Argument(help="The fact or convention to store.")],
    user: Annotated[bool, typer.Option("--user", help="Store in the user (global) pool.")] = False,
    project: Annotated[
        bool, typer.Option("--project", help="Store in the current project pool.")
    ] = False,
    stream: Annotated[
        str | None,
        typer.Option("--stream", help="Store in a named stream's pool.", metavar="NAME"),
    ] = None,
    tag: Annotated[
        list[str] | None,
        typer.Option("--tag", help="Tag for the entry; repeat for several."),
    ] = None,
) -> None:
    """Add a context entry."""
    if stream is not None and (user or project):
        raise typer.BadParameter("--stream cannot be combined with --user/--project.")
    try:
        entry = context_service.add_entry(
            text, pool=resolve_pool(user, project), tags=tag or [], stream=stream
        )
    except context_service.HomeProjectRefused as exc:
        fail(str(exc), error="home_is_not_a_project", ref=str(exc.root))
    except LookupError as exc:
        fail(str(exc), error="unknown_stream", ref=stream or "")
    emit_entry(entry, verb="added")


@app.command("show")
def show(entry_id: EntryId) -> None:
    """Show a context entry in full."""
    try:
        entry = context_service.show_entry(entry_id)
    except (KeyError, AmbiguousIdError) as exc:
        _fail_lookup(entry_id, exc)
    emit_entry_detail(entry)


@app.command("edit")
def edit(entry_id: EntryId) -> None:
    """Edit a context entry in your editor."""
    try:
        entry = context_service.edit_entry(entry_id)
    except (KeyError, AmbiguousIdError) as exc:
        _fail_lookup(entry_id, exc)
    emit_entry(entry, verb="updated")


@app.command("remove")
def remove(entry_id: EntryId) -> None:
    """Delete a context entry."""
    try:
        context_service.remove_entry(entry_id)
    except (KeyError, AmbiguousIdError) as exc:
        _fail_lookup(entry_id, exc)
    emit_removed(entry_id)


@app.command("search")
def search(query: Annotated[str, typer.Argument(help="Search text.")]) -> None:
    """Search context entries by text and tags."""
    results = context_service.search_entries(query)
    emit_entries(results, empty_message=f"No entries match '{query}'.")


@app.command("preview")
def preview() -> None:
    """Preview what would be injected into an agent session right now."""
    emit_block(context_service.preview())


@app.command("import")
def import_(
    file: Annotated[Path, typer.Argument(help="Markdown or JSON file to import.")],
) -> None:
    """Import context entries from a file."""
    try:
        count = context_service.import_entries(file)
    except FileNotFoundError:
        fail(f"no such file: {file}", error="file_not_found", ref=str(file))
    except ValueError as exc:
        fail(f"could not import {file}: {exc}", error="invalid_file", ref=str(file))
    emit_imported(count)


@app.command("export")
def export(
    file: Annotated[Path | None, typer.Argument(help="Destination file (default: stdout).")] = None,
    format_: Annotated[
        ExportFormat, typer.Option("--format", help="Output format.")
    ] = ExportFormat.md,
) -> None:
    """Export context entries to Markdown or JSON."""
    context_service.export_entries(file, fmt=format_)
    if file is not None:
        emit_exported(file)


@app.command("promote")
def promote(entry_id: EntryId) -> None:
    """Promote a project entry into the user pool."""
    try:
        entry = context_service.promote_entry(entry_id)
    except (KeyError, AmbiguousIdError) as exc:
        _fail_lookup(entry_id, exc)
    except ValueError as exc:
        fail(str(exc), error="not_promotable", ref=entry_id)
    emit_entry(entry, verb="promoted")
