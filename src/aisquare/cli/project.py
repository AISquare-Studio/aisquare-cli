"""``aisquare project`` (alias ``workspace``) — manage projects."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from aisquare.cli.common import (
    emit_onboard,
    emit_project_action,
    emit_project_detail,
    emit_project_forget,
    emit_projects,
    emit_prune,
    fail,
)
from aisquare.core.console import stdout_console
from aisquare.core.state import get_state
from aisquare.services import project as project_service

app = typer.Typer(
    help="Inspect, switch and onboard projects (alias: workspace).", no_args_is_help=True
)


@app.command("info")
def info() -> None:
    """Show the active project: id, root and linked repos."""
    emit_project_detail(project_service.info())


@app.command("list")
def list_(
    all: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Include the directories hooked sessions merely ran in (captured, not added).",
        ),
    ] = False,
) -> None:
    """List your projects (the active one is marked with *).

    A directory a hooked Claude Code session ran in is CAPTURED — its prompt
    history and injected memory work there — but it is listed, here and in the
    fleet sidebar, only once something adds it on purpose: `init`, `project
    onboard`, `project link`, the sidebar's +, `team on`, a fleet spawn.
    `--all` shows the captured ones too, marked.
    """
    projects = project_service.list_projects(all=all)
    emit_projects(projects, active_id=project_service.info().id)


@app.command("switch")
def switch(name: Annotated[str, typer.Argument(help="Project name or id prefix.")]) -> None:
    """Switch the active project."""
    try:
        project = project_service.switch(name)
    except KeyError:
        fail(f"no project matches '{name}'", error="not_found", ref=name)
    except ValueError as exc:
        fail(str(exc), error="ambiguous_project", ref=name)
    emit_project_action(f"✓ switched to {project.root.name or project.id} ({project.id})", project)


@app.command("link")
def link(repo: Annotated[str, typer.Argument(help="Repository path or URL to link.")]) -> None:
    """Link another repository into the active project."""
    project = project_service.link(repo)
    emit_project_action(f"✓ linked {repo} into {project.root.name or project.id}", project)


@app.command("onboard")
def onboard(
    path: Annotated[
        Path | None, typer.Argument(help="Project root (default: current directory).")
    ] = None,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Re-scan even if already onboarded.")
    ] = False,
) -> None:
    """Pack the codebase into a snapshot and seed its context pool."""
    emit_onboard(project_service.onboard(path, refresh=refresh))


_PURGE_HELP = (
    "Also delete the project's context entries, prompt history, board rows, turn "
    "metrics and snapshot. Without it they stay in the store, hidden, and come back if the "
    "root is registered again."
)


@app.command("forget")
def forget(
    ref: Annotated[str, typer.Argument(help="Project id prefix, name, codename or path.")],
    purge: Annotated[bool, typer.Option("--purge", help=_PURGE_HELP)] = False,
) -> None:
    """Remove a project registration. Refused while it has live fleet agents."""
    try:
        report = project_service.forget(ref, purge=purge)
    except KeyError:
        fail(f"no project matches '{ref}'", error="not_found", ref=ref)
    except ValueError as exc:
        fail(str(exc), error="ambiguous_project", ref=ref)
    except project_service.ProjectBusyError as exc:
        fail(str(exc), error="project_busy", ref=exc.project.id, exit_code=2)
    emit_project_forget(report)


@app.command("prune")
def prune(
    missing: Annotated[
        bool,
        typer.Option("--missing", help="Drop registrations whose root no longer exists on disk."),
    ] = False,
    worktrees: Annotated[
        bool,
        typer.Option(
            "--worktrees",
            help="Drop registrations whose root is a git worktree of another registered project.",
        ),
    ] = False,
    purge: Annotated[bool, typer.Option("--purge", help=_PURGE_HELP)] = False,
    captured_only: Annotated[
        bool,
        typer.Option(
            "--captured-only",
            help="Drop directories a session merely ran in (never added on purpose) that hold "
            "no context entries and were last touched more than --older-than days ago.",
        ),
    ] = False,
    older_than: Annotated[
        int,
        typer.Option("--older-than", min=0, help="Days of inactivity for --captured-only."),
    ] = 30,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Drop without asking; required off a terminal.")
    ] = False,
) -> None:
    """Drop stale registrations: missing roots, worktrees — both when neither is given.

    `--captured-only` is the third reason (#139): the scratch directories a
    hooked session captured that nothing ever added on purpose, with no context
    entries and nothing touched in `--older-than` days (30 by default). Prints
    what it would drop and asks first at a terminal. Off a terminal it is a dry
    run unless --yes; under --json without --yes it lists the candidates and
    changes nothing.
    """
    if not missing and not worktrees and not captured_only:
        missing = worktrees = True
    candidates = project_service.prune_candidates(
        missing=missing,
        worktrees=worktrees,
        captured_older_than=older_than if captured_only else None,
    )
    if yes:
        emit_prune(project_service.prune(candidates, purge=purge))
        return
    plan = project_service.prune_plan(candidates, purge=purge)
    emit_prune(plan)
    droppable = len(plan.candidates) - len(plan.kept)
    if get_state().json_output or droppable == 0:
        return
    noun = "registration" if droppable == 1 else "registrations"
    if not sys.stdin.isatty():
        stdout_console().print(
            f"dry run: nothing dropped — re-run with --yes to drop {droppable} {noun}"
        )
        return
    if not typer.confirm(f"Drop {droppable} {noun}?", default=False):
        stdout_console().print("nothing dropped")
        return
    emit_prune(project_service.prune(candidates, purge=purge))
