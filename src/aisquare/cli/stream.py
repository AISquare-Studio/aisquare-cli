"""``aisquare stream`` — named bodies of work spanning several projects."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from aisquare.cli.common import (
    emit_stream_action,
    emit_stream_detail,
    emit_streams,
    fail,
)
from aisquare.core.store import AmbiguousIdError
from aisquare.core.workspace import HomeProjectRefused
from aisquare.services import stream as stream_service
from aisquare.services.stream import StreamCycleError, UnknownStreamError

app = typer.Typer(
    help="Group projects into streams of work; a project can be in several.",
    no_args_is_help=True,
)

StreamName = Annotated[str, typer.Argument(help="Stream name (or id prefix).")]


@app.command("new")
def new(
    name: Annotated[str, typer.Argument(help="Name for the new stream.")],
    requires: Annotated[
        list[str] | None,
        typer.Option("--requires", help="Existing stream this one depends on; repeatable."),
    ] = None,
) -> None:
    """Create a stream."""
    try:
        stream = stream_service.create(name, requires or [])
    except UnknownStreamError as exc:
        fail(str(exc), error="unknown_stream", ref=exc.ref)
    except StreamCycleError as exc:
        fail(str(exc), error="stream_cycle", ref=name)
    except ValueError as exc:
        fail(str(exc), error="duplicate_stream", ref=name)
    emit_stream_action(f"✓ created stream {stream.name}", stream)


@app.command("add")
def add(
    name: StreamName,
    paths_: Annotated[
        list[Path] | None,
        typer.Argument(help="Paths whose projects join the stream (default: here)."),
    ] = None,
) -> None:
    """Add the projects containing PATHS (default: the current one) to a stream."""
    try:
        stream, added = stream_service.add_members(name, paths_ or [])
    except (UnknownStreamError, AmbiguousIdError) as exc:
        fail(str(exc), error="unknown_stream", ref=name)
    except HomeProjectRefused as exc:
        fail(
            f"{exc} — for a docs-only directory, create a marker first: "
            "mkdir <dir>/.aisquare, then re-add it",
            error="home_is_not_a_project",
            ref=str(exc.root),
        )
    roots = ", ".join(str(project.root) for project in added)
    emit_stream_action(f"✓ {stream.name} now includes: {roots}", stream)


@app.command("remove")
def remove(
    name: StreamName,
    path: Annotated[Path, typer.Argument(help="Path whose project leaves the stream.")],
) -> None:
    """Remove the project containing PATH from a stream."""
    try:
        stream, project = stream_service.remove_member(name, path)
    except (UnknownStreamError, AmbiguousIdError) as exc:
        fail(str(exc), error="unknown_stream", ref=name)
    emit_stream_action(f"✓ removed {project.root} from {stream.name}", stream)


@app.command("requires")
def requires(
    name: StreamName,
    required: Annotated[
        list[str], typer.Argument(help="Streams this one depends on (their entries join scope).")
    ],
) -> None:
    """Make a stream depend on other streams."""
    try:
        stream = stream_service.require(name, required)
    except (UnknownStreamError, AmbiguousIdError) as exc:
        fail(str(exc), error="unknown_stream", ref=name)
    except StreamCycleError as exc:
        fail(str(exc), error="stream_cycle", ref=name)
    emit_stream_action(f"✓ {stream.name} requires: {', '.join(required)}", stream)


@app.command("list")
def list_() -> None:
    """List streams."""
    emit_streams(stream_service.list_streams())


@app.command("show")
def show(name: StreamName) -> None:
    """Show a stream: members, requirements and entry count."""
    try:
        stream, members, required_names, entry_count = stream_service.show(name)
    except (UnknownStreamError, AmbiguousIdError) as exc:
        fail(str(exc), error="unknown_stream", ref=name)
    emit_stream_detail(stream, members, required_names, entry_count)
