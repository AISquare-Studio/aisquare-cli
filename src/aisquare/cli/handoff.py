"""``aisquare handoff`` — give an agent the context of specific past sessions."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from aisquare.cli.common import fail
from aisquare.core.console import stdout_console
from aisquare.core.state import get_state
from aisquare.services import handoff as handoff_service
from aisquare.services.handoff import AmbiguousSessionError, SessionNotFoundError


def handoff(
    session_ids: Annotated[
        list[str],
        typer.Argument(help="Claude Code session id(s) — prefixes fine — to distill."),
    ],
    to_role: Annotated[
        str | None,
        typer.Option("--to", help="Team role to address the handoff note to.", metavar="ROLE"),
    ] = None,
    task: Annotated[
        str | None,
        typer.Option("--task", help="Team task to attach the handoff note to.", metavar="ID"),
    ] = None,
    raw: Annotated[
        bool,
        typer.Option("--raw", help="Also keep each session's redacted transcript digest."),
    ] = False,
    no_llm: Annotated[
        bool,
        typer.Option("--no-llm", help="Skip the model call; produce structural briefs only."),
    ] = False,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out", help="Write the combined bundle here (default: ~/.aisquare/handoffs)."
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
    """Distill past sessions into state-of-play briefs another agent can start from."""
    try:
        report = handoff_service.handoff(
            session_ids,
            claude_projects=claude_dir,
            to_role=to_role,
            task_ref=task,
            raw=raw,
            use_llm=not no_llm,
            out=out,
        )
    except SessionNotFoundError as exc:
        fail(str(exc), error="session_not_found", ref=exc.ref)
    except AmbiguousSessionError as exc:
        fail(str(exc), error="ambiguous_session", ref=exc.ref)
    if get_state().json_output:
        typer.echo(report.model_dump_json())
        return
    console = stdout_console()
    for brief in report.briefs:
        mode = "distilled" if brief.distilled else "structural"
        console.print(f"✓ {brief.session_id} ({brief.turns} turns, {mode}): {brief.brief_path}")
    console.print(f"✓ bundle: {report.bundle_path}")
    if report.note_posted:
        console.print("✓ note posted to the team pipe")
    elif report.note_skipped_reason:
        console.print(f"⚠ {report.note_skipped_reason}")
