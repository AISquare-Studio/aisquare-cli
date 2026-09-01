"""``aisquare import`` — bring context in from where it already lives."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from aisquare.cli.common import fail
from aisquare.core.console import stdout_console
from aisquare.core.state import get_state
from aisquare.services import claude_import as claude_import_service
from aisquare.services.stream import UnknownStreamError

app = typer.Typer(
    help="Import context from other tools' stores (Claude Code auto-memory, …).",
    no_args_is_help=True,
)


@app.command("claude-memory")
def claude_memory(
    stream: Annotated[
        str | None,
        typer.Option(
            "--stream",
            help="Stream to receive project/reference memories (user/feedback "
            "always go to the user pool).",
            metavar="NAME",
        ),
    ] = None,
    claude_dir: Annotated[
        Path | None,
        typer.Option(
            "--claude-dir",
            help="Claude Code projects directory (default: ~/.claude/projects).",
        ),
    ] = None,
) -> None:
    """Import Claude Code's auto-memory files into aisquare's pools.

    Idempotent: files already imported (matched by their ``claude-memory:``
    tag) are skipped, so re-running after new memories appear imports exactly
    the new ones. No model call — the files are already curated.
    """
    projects = claude_dir or claude_import_service.default_claude_projects()
    try:
        report = claude_import_service.import_memory(projects, stream=stream)
    except FileNotFoundError as exc:
        fail(str(exc), error="claude_projects_not_found", ref=str(projects))
    except UnknownStreamError as exc:
        fail(str(exc), error="unknown_stream", ref=exc.ref)
    if get_state().json_output:
        typer.echo(report.model_dump_json())
        return
    console = stdout_console()
    for entry in report.imported:
        first = entry.text.splitlines()[0] if entry.text else ""
        console.print(f"✓ imported ({entry.pool}): {first}")
    if report.skipped:
        console.print(f"- skipped {len(report.skipped)} already imported")
    if not report.imported and not report.skipped:
        console.print(f"No memory files found under {projects}.")
