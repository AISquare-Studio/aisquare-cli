"""Shared CLI parsing and rendering helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import NoReturn

import typer
from rich.table import Table

from aisquare.core.console import stderr_console, stdout_console
from aisquare.core.state import get_state
from aisquare.models import ContextEntry, InjectionRecord, Pool

_DEFAULT_EMPTY = 'No context entries yet. Add one with: aisquare remember "…"'


def resolve_pool(user: bool, project: bool) -> Pool | None:
    """Map the ``--user``/``--project`` flag pair onto a pool name.

    Returns ``None`` when neither flag is given, letting the service apply
    the configured default pool.
    """
    if user and project:
        raise typer.BadParameter("--user and --project are mutually exclusive.")
    if user:
        return "user"
    if project:
        return "project"
    return None


def emit_entry(entry: ContextEntry, *, verb: str) -> None:
    """Render a single entry — JSON under ``--json``, a confirmation otherwise."""
    if get_state().json_output:
        typer.echo(entry.model_dump_json())
    else:
        stdout_console().print(f"✓ {verb} ({entry.pool}): {entry.text}")


def emit_entries(entries: list[ContextEntry], *, empty_message: str = _DEFAULT_EMPTY) -> None:
    """Render a list of entries — a JSON array under ``--json``, a table otherwise."""
    if get_state().json_output:
        typer.echo(json.dumps([entry.model_dump(mode="json") for entry in entries]))
        return
    if not entries:
        stdout_console().print(empty_message)
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", no_wrap=True)
    table.add_column("POOL", no_wrap=True)
    table.add_column("TAGS")
    table.add_column("TEXT")
    for entry in entries:
        table.add_row(entry.id, entry.pool, ", ".join(entry.tags), entry.text)
    stdout_console().print(table)


def emit_removed(ref: str) -> None:
    """Confirm a deletion — JSON under ``--json``, a confirmation otherwise."""
    if get_state().json_output:
        typer.echo(json.dumps({"removed": ref}, separators=(",", ":")))
    else:
        stdout_console().print(f"✓ removed {ref}")


def emit_imported(count: int) -> None:
    """Confirm an import — JSON under ``--json``, a confirmation otherwise."""
    if get_state().json_output:
        typer.echo(json.dumps({"imported": count}, separators=(",", ":")))
    else:
        noun = "entry" if count == 1 else "entries"
        stdout_console().print(f"✓ imported {count} {noun}")


def emit_exported(file: Path) -> None:
    """Confirm an export to a file — JSON under ``--json``, a confirmation otherwise."""
    if get_state().json_output:
        typer.echo(json.dumps({"exported": str(file)}, separators=(",", ":")))
    else:
        stdout_console().print(f"✓ exported to {file}")


def emit_entry_detail(entry: ContextEntry) -> None:
    """Render one entry in full — JSON under ``--json``, a key/value view otherwise."""
    if get_state().json_output:
        typer.echo(entry.model_dump_json())
        return
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    grid.add_row("id", entry.id)
    grid.add_row("pool", entry.pool)
    if entry.project_id:
        grid.add_row("project", entry.project_id)
    if entry.tags:
        grid.add_row("tags", ", ".join(entry.tags))
    grid.add_row("created", entry.created_at.isoformat())
    grid.add_row("updated", entry.updated_at.isoformat())
    console = stdout_console()
    console.print(grid)
    console.print()
    console.print(entry.text)


def emit_block(block: str) -> None:
    """Emit an assembled context block — wrapped under ``--json``, verbatim otherwise."""
    if get_state().json_output:
        typer.echo(json.dumps({"block": block}))
    else:
        typer.echo(block, nl=False)


def emit_injection_record(record: InjectionRecord | None) -> None:
    """Render the last-injection record for ``why``."""
    if get_state().json_output:
        typer.echo("null" if record is None else record.model_dump_json())
        return
    console = stdout_console()
    if record is None:
        console.print("No context has been injected yet. Run: aisquare inject")
        return
    total = record.user_count + record.project_count
    console.print(f"Last injection: {record.injected_at.isoformat()}")
    console.print(
        f"  {total} entries — {record.user_count} from your user pool, "
        f"{record.project_count} from this project"
    )


def fail(message: str, *, error: str, ref: str | None = None, exit_code: int = 1) -> NoReturn:
    """Report a runtime error and exit.

    Mirrors the stub contract: a machine-readable object on stdout under
    ``--json``, a human message on stderr otherwise.
    """
    if get_state().json_output:
        payload = {"error": error}
        if ref is not None:
            payload["ref"] = ref
        typer.echo(json.dumps(payload, separators=(",", ":")))
    else:
        stderr_console().print(f"✗ {message}")
    raise typer.Exit(exit_code)
