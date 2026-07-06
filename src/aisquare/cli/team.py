"""``aisquare team`` — the shared working-memory bus for parallel sessions.

Also home of the top-level ``note`` and ``board`` shortcuts (registered by
``cli/app.py``) so agents can type the two most frequent verbs directly.
"""

from __future__ import annotations

import json
from typing import Annotated, NoReturn

import typer

from aisquare.cli.common import fail
from aisquare.core.console import stdout_console
from aisquare.core.state import get_state
from aisquare.core.store import AmbiguousIdError
from aisquare.services import team as team_service
from aisquare.services.team import TeamDisabledError

app = typer.Typer(help="Coordinate parallel agent sessions on this project.", no_args_is_help=True)

SessionRef = Annotated[
    str | None,
    typer.Option("--as", help="Act as this team session (id prefix, from your board)."),
]


def _fail_team(exc: Exception, ref: str | None = None) -> NoReturn:
    """Translate service errors into the shared CLI error contract."""
    if isinstance(exc, TeamDisabledError):
        fail(str(exc), error="team_disabled")
    if isinstance(exc, AmbiguousIdError):
        fail(f"'{exc.ref}' is ambiguous — use more characters", error="ambiguous_id", ref=exc.ref)
    if isinstance(exc, KeyError):
        missing = ref if ref is not None else str(exc)
        fail(f"nothing matches '{missing}'", error="not_found", ref=missing)
    raise exc


@app.command("on")
def on() -> None:
    """Activate the team bus for this project."""
    try:
        project = team_service.activate()
    except TeamDisabledError as exc:
        _fail_team(exc)
    if get_state().json_output:
        typer.echo(json.dumps({"activated": project.id, "root": str(project.root)}))
    else:
        stdout_console().print(
            f"✓ team bus active for {project.root.name or project.id} — "
            "sessions launched here now share tasks and notes"
        )


@app.command("status")
def status() -> None:
    """Show the live team board (sessions, tasks, recent updates)."""
    board()


@app.command("focus")
def focus(
    text: Annotated[str, typer.Argument(help="What this session is working on right now.")],
    as_session: SessionRef = None,
) -> None:
    """Announce this session's current focus to the team."""
    if as_session is None:
        fail("--as <session> is required (your id is on the board)", error="missing_session")
    try:
        session = team_service.set_focus(text, as_session)
    except (TeamDisabledError, KeyError, AmbiguousIdError) as exc:
        _fail_team(exc, as_session)
    if get_state().json_output:
        typer.echo(session.model_dump_json())
    else:
        stdout_console().print(
            f"✓ focus of {team_service.short_id(session.id)}: {text}", markup=False
        )


@app.command("role")
def role(
    name: Annotated[str, typer.Argument(help="Role for this session (planner/coder/runner/…).")],
    as_session: SessionRef = None,
) -> None:
    """Set a session's role on the board."""
    if as_session is None:
        fail("--as <session> is required (your id is on the board)", error="missing_session")
    try:
        session = team_service.set_role(name, as_session)
    except (TeamDisabledError, KeyError, AmbiguousIdError) as exc:
        _fail_team(exc, as_session)
    if get_state().json_output:
        typer.echo(session.model_dump_json())
    else:
        stdout_console().print(f"✓ {team_service.short_id(session.id)} is now {session.role}")


@app.command("log")
def log(
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many events to show.")] = 30,
) -> None:
    """Show the team pipe: recent events from every session."""
    try:
        events = team_service.log_events(limit=limit)
    except TeamDisabledError as exc:
        _fail_team(exc)
    if get_state().json_output:
        typer.echo(json.dumps([event.as_envelope().model_dump(mode="json") for event in events]))
        return
    if not events:
        stdout_console().print("No team events yet.")
        return
    console = stdout_console()
    for event in events:
        who = team_service.short_id(event.session_id) if event.session_id else "cli"
        console.print(f"{event.created_at:%H:%M} {who} {event.kind}: {event.text}", markup=False)


@app.command("distill")
def distill() -> None:
    """Push undistilled decisions/results/outcomes into the project brain now."""
    try:
        count = team_service.distill_now()
    except TeamDisabledError as exc:
        _fail_team(exc)
    if get_state().json_output:
        typer.echo(json.dumps({"distilled": count}))
    elif count is None:
        stdout_console().print("… another distill is already running — it has the brain")
    else:
        noun = "page" if count == 1 else "pages"
        stdout_console().print(f"✓ distilled {count} {noun} into the project brain")


def recall(
    query: Annotated[str, typer.Argument(help="What to look up in the project brain.")],
) -> None:
    """Search the team's long-term memory (decisions, results, outcomes)."""
    try:
        output = team_service.recall(query)
    except TeamDisabledError as exc:
        _fail_team(exc)
    if output is None:
        fail(
            "project brain unavailable — gbrain missing, brain busy, or nothing "
            "distilled yet (try `aisquare team distill`)",
            error="brain_unavailable",
        )
    if get_state().json_output:
        typer.echo(json.dumps({"query": query, "output": output}))
    else:
        stdout_console().print(output.rstrip("\n"), markup=False)


def note(
    text: Annotated[str, typer.Argument(help="The note/decision/question/result to share.")],
    as_session: SessionRef = None,
    task: Annotated[
        str | None, typer.Option("--task", help="Task this note is about (id prefix).")
    ] = None,
    to: Annotated[
        str | None, typer.Option("--to", help="Address a role (planner/coder/runner/…).")
    ] = None,
    kind: Annotated[
        str, typer.Option("--kind", help="note, decision, question or result.")
    ] = "note",
) -> None:
    """Share a note with the team (it reaches every session automatically)."""
    try:
        event = team_service.add_note(
            text, session_ref=as_session, task_ref=task, to_role=to, kind=kind
        )
    except (TeamDisabledError, KeyError, AmbiguousIdError) as exc:
        _fail_team(exc, task or as_session)
    if get_state().json_output:
        typer.echo(event.as_envelope().model_dump_json())
    else:
        stdout_console().print(f"✓ shared ({event.kind}): {event.text}", markup=False)


def board(
    watch: Annotated[
        bool, typer.Option("--watch", "-w", help="Full-screen live board; Ctrl-C exits.")
    ] = False,
    interval: Annotated[
        float, typer.Option("--interval", "-i", help="Refresh seconds in watch mode.")
    ] = 3.0,
) -> None:
    """Show the live team board (sessions, tasks, recent updates)."""
    if watch:
        if get_state().json_output:
            raise typer.BadParameter("--watch and --json cannot be combined")
        _watch_board(max(interval, 0.5))
        return
    try:
        project, sessions, tasks, events = team_service.board_data()
    except TeamDisabledError as exc:
        _fail_team(exc)
    if get_state().json_output:
        typer.echo(
            json.dumps(
                {
                    "project": project.model_dump(mode="json"),
                    "sessions": [s.model_dump(mode="json") for s in sessions],
                    "tasks": [t.model_dump(mode="json") for t in tasks],
                    "events": [e.as_envelope().model_dump(mode="json") for e in events],
                }
            )
        )
        return
    if not sessions and not tasks:
        stdout_console().print(
            "Team bus is quiet here. Activate with `aisquare team on`, or launch a "
            "session with AISQUARE_ROLE=planner (coder/runner/…) set."
        )
        return
    stdout_console().print(
        team_service.render_board(project, sessions, tasks, events), markup=False
    )


def _watch_board(interval: float) -> None:
    """Run the live board (interactive TUI or Rich fallback — see cli.watch)."""
    from aisquare.cli import watch as watch_ui

    try:
        watch_ui.run_watch(interval)
    except TeamDisabledError as exc:
        _fail_team(exc)
