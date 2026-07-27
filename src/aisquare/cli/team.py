"""``aisquare team`` — the agent orchestrator for parallel sessions.

Also home of the top-level ``note`` and ``board`` shortcuts (registered by
``cli/app.py``) so agents can type the two most frequent verbs directly.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
from typing import Annotated, NoReturn

import typer

from aisquare.cli.common import fail, local_time
from aisquare.core import harness, orchestrator
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
    """Activate the orchestrator for this project."""
    try:
        project = team_service.activate()
    except TeamDisabledError as exc:
        _fail_team(exc)
    if get_state().json_output:
        typer.echo(json.dumps({"activated": project.id, "root": str(project.root)}))
    else:
        stdout_console().print(
            f"✓ agent orchestrator active for {project.root.name or project.id} — "
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


@app.command("spawn")
def spawn(
    role_name: Annotated[
        str, typer.Argument(help="Role to launch (planner/coder/runner/validator/…).")
    ],
    execute: Annotated[
        bool,
        typer.Option(
            "--exec",
            help="Replace this process with the launched session (default: print the command).",
        ),
    ] = False,
    probe: Annotated[
        bool | None,
        typer.Option(
            "--probe/--no-probe",
            help="Verify model availability before picking (paid ~1-token probe, cached 24h). "
            "Default: probe, unless AISQUARE_HARNESS_PROBE=0.",
        ),
    ] = None,
    refresh: Annotated[
        bool,
        typer.Option(
            "--refresh",
            help="Ignore cached availability verdicts (use after an entitlement changes).",
        ),
    ] = False,
    effort: Annotated[
        str | None,
        typer.Option(
            "--effort",
            help="Effort for this role (low/medium/high/xhigh/max/ultracode). Absolute — "
            "skips the role offset. Default: the session base (AISQUARE_EFFORT, else "
            "CLAUDE_EFFORT, else high) shifted by the role's offset.",
        ),
    ] = None,
) -> None:
    """Resolve a role's model down its ladder and launch (or print) the session.

    The harness picks the strongest available model for the role — e.g. the
    planner wants fable and falls back to opus, then sonnet, when the account
    doesn't serve it — and passes the role's effort level. Resolution never
    blocks a launch: on any doubt it degrades down the ladder.
    """
    if not orchestrator.team_enabled():
        _fail_team(TeamDisabledError())
    if effort is not None and harness.normalize_effort(effort) is None:
        fail(
            f"unknown --effort '{effort}' — use one of: "
            f"{', '.join([*harness.EFFORT_SCALE, harness.ULTRACODE])}",
            error="bad_effort",
        )
    resolution = harness.resolve_model(role_name, probe=probe, refresh=refresh, effort=effort)
    env_pairs = [f"AISQUARE_ROLE={shlex.quote(role_name)}"]
    if resolution is None:
        argv = ["claude"]
        banner = f"{role_name}: untiered role — launching on the session default model"
    else:
        argv = ["claude", "--model", resolution.model, "--effort", resolution.effort]
        skipped = f" (skipped: {', '.join(resolution.skipped)})" if resolution.skipped else ""
        profile = harness.ROLE_PROFILES.get(role_name)
        mission = profile.mission if profile else "pinned by AISQUARE_MODEL override"
        banner = (
            f"{role_name}: {resolution.model} @ {resolution.effort} "
            f"[model {resolution.source} · effort {resolution.effort_source}]{skipped} — {mission}"
        )
    command = " ".join([*env_pairs, shlex.join(argv)])
    if get_state().json_output:
        typer.echo(
            json.dumps(
                {
                    "role": role_name,
                    "model": resolution.model if resolution else None,
                    "effort": resolution.effort if resolution else None,
                    "source": resolution.source if resolution else "untiered",
                    "effort_source": resolution.effort_source if resolution else None,
                    "skipped": resolution.skipped if resolution else [],
                    "command": command,
                }
            )
        )
        if not execute:
            return
    else:
        stdout_console().print(f"✓ {banner}", markup=False)
        if resolution is not None:
            caution = harness.effort_warning(resolution.model, resolution.effort)
            if caution:
                stdout_console().print(f"  ⚠ {caution}", markup=False)
    if execute:
        if shutil.which("claude") is None:
            fail("claude not found on PATH", error="claude_missing")
        env = dict(os.environ)
        env["AISQUARE_ROLE"] = role_name
        os.execvpe(argv[0], argv, env)
    if not get_state().json_output:
        stdout_console().print(f"  run it in the role's terminal:\n  {command}", markup=False)


@app.command("harness")
def harness_status() -> None:
    """Show the role→model matrix and how each ladder resolves right now."""
    if not orchestrator.team_enabled():
        _fail_team(TeamDisabledError())
    rows = []
    base, base_source = harness.base_effort()
    for name, profile in harness.ROLE_PROFILES.items():
        resolution = harness.resolve_model(name, probe=False)
        assert resolution is not None  # every profiled role resolves
        pinned = harness.role_model_override(name)
        rows.append(
            {
                "role": name,
                "ladder": profile.ladder,
                "effort_offset": profile.effort_offset,
                "effort": resolution.effort,
                "effort_source": resolution.effort_source,
                "resolves_to": pinned or resolution.model,
                "source": "pinned" if pinned else resolution.source,
                "mission": profile.mission,
            }
        )
    interference = harness.interfering_env()
    if get_state().json_output:
        typer.echo(
            json.dumps(
                {
                    "base_effort": base,
                    "base_effort_source": base_source,
                    "roles": rows,
                    "interfering_env": interference,
                }
            )
        )
        return
    console = stdout_console()
    console.print(f"base effort: {base} ({base_source})", markup=False)
    for name, profile in harness.ROLE_PROFILES.items():
        resolution = harness.resolve_model(name, probe=False)
        assert resolution is not None  # every profiled role resolves
        pinned = harness.role_model_override(name)
        ladder = "→".join(profile.ladder)
        offset = f"+{profile.effort_offset}" if profile.effort_offset else " 0"
        source = "pinned" if pinned else resolution.source
        console.print(
            f"{name:<10} {ladder:<20} effort={resolution.effort:<10}({offset}) "
            f"→ {pinned or resolution.model} [{source}]",
            markup=False,
        )
    if interference:
        console.print(f"⚠ env overrides model selection: {', '.join(interference)}", markup=False)
    console.print(
        "Resolution shown without probing — `aisquare team spawn <role>` verifies live.",
        markup=False,
    )


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
        console.print(
            f"{local_time(event.created_at):%H:%M} {who} {event.kind}: {event.text}",
            markup=False,
        )


@app.command("distill")
def distill(
    rescan: Annotated[
        bool,
        typer.Option(
            "--all", help="Backfill: re-distill the whole pipe from the beginning (idempotent)."
        ),
    ] = False,
) -> None:
    """Push undistilled notes/decisions/outcomes into the project brain now."""
    try:
        count = team_service.distill_now(rescan=rescan)
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
            "The orchestrator is quiet here. Activate with `aisquare team on`, or launch a "
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
