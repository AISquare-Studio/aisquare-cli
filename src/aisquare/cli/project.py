"""``aisquare project`` (alias ``workspace``) — manage projects."""

from __future__ import annotations

import json
import shlex
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
from aisquare.core.store import store_session
from aisquare.core.workspace import find_project_root, project_id_for
from aisquare.models import ProjectInfo
from aisquare.services import project as project_service
from aisquare.services import project_groups as groups_service

app = typer.Typer(
    help="Inspect, switch, onboard and arrange projects (alias: workspace).", no_args_is_help=True
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
    group: Annotated[
        str | None, typer.Option("--group", help="Only the members of this group.")
    ] = None,
    pinned: Annotated[bool, typer.Option("--pinned", help="Only the pinned projects.")] = False,
) -> None:
    """List your projects (the active one is marked with *), in the order the sidebar shows.

    A directory a hooked Claude Code session ran in is CAPTURED — its prompt
    history and injected memory work there — but it is listed, here and in the
    fleet sidebar, only once something adds it on purpose: `init`, `project
    onboard`, `project link`, the sidebar's +, `team on`, a fleet spawn.
    `--all` shows the captured ones too, marked.
    """
    with store_session() as store:
        arrangement = groups_service.load_arrangement(store, all=all)
        group_names = {g.id: g.name for g in store.project_groups()}
        try:
            chosen = groups_service.resolve_group(store, group) if group else None
        except KeyError:
            fail(f"no group matches '{group}'", error="not_found", ref=str(group))
        listed = arrangement.ordered_projects()
        # Counted only when nothing is listed at all, where "nothing registered"
        # would be wrong — not when --group or --pinned filtered the list empty.
        hidden = 0 if all or listed else len(store.captured_projects())
    projects = listed
    if chosen is not None:
        projects = [p for p in projects if p.group_id == chosen.id]
    if pinned:
        projects = [p for p in projects if p.pinned_at is not None]
    filtered = None
    if listed and not projects:
        # The filter matched nothing in a list that has rows, and the empty table
        # said "No projects registered yet. Run: aisquare init" (review of #171,
        # round 1). It names the filter instead, and the step that fills it.
        where = f" in group {chosen.name}" if chosen is not None else ""
        if pinned:
            filtered = f"No pinned projects{where} — pin one: aisquare project pin <project>"
        elif chosen is not None:
            filtered = (
                f"No projects{where} — add one: aisquare project group add "
                f"{shlex.quote(chosen.name)} <project>"
            )
    emit_projects(
        projects,
        active_id=project_service.info().id,
        hidden=hidden,
        group_names=group_names,
        filtered=filtered,
    )


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
    group: Annotated[
        str | None, typer.Option("--group", help="Put the project in this group (created if new).")
    ] = None,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Re-scan even if already onboarded.")
    ] = False,
) -> None:
    """Pack the codebase into a snapshot and seed its context pool."""
    report = project_service.onboard(path, refresh=refresh)
    if group:
        project_id = project_id_for(find_project_root(path or Path.cwd()))
        with store_session() as store:
            try:
                target = groups_service.resolve_group(store, group)
            except KeyError:
                target, _ = groups_service.create_group(store, group)
            groups_service.add_to_group(store, target.id, [project_id])
    emit_onboard(report)


_PURGE_HELP = (
    "Also delete the project's context entries, prompt history, board rows, turn "
    "metrics and snapshot. Without it they stay in the store, hidden, and come back if the "
    "root is registered again."
)


_STALE_CAPTURE_DAYS = 30
"""How long a captured directory sits untouched before ``prune --captured-only`` takes it."""


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
        int | None,
        typer.Option(
            "--older-than",
            min=0,
            help=f"Days of inactivity for --captured-only (default {_STALE_CAPTURE_DAYS}).",
        ),
    ] = None,
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
    if older_than is not None and not captured_only:
        # Ignored silently, `prune --older-than 7` read as "what is older than a
        # week" and swept every missing root and worktree instead.
        fail("--older-than applies only with --captured-only", error="usage")
    if not missing and not worktrees and not captured_only:
        missing = worktrees = True
    days = _STALE_CAPTURE_DAYS if older_than is None else older_than
    candidates = project_service.prune_candidates(
        missing=missing,
        worktrees=worktrees,
        captured_older_than=days if captured_only else None,
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


# --- groups, pins and manual order (#140) ---------------------------------------------------

group_app = typer.Typer(
    help="Project groups: a named, collapsible container for listing and managing — "
    "nothing inside a project is shared.",
    no_args_is_help=True,
)
app.add_typer(group_app, name="group")


def _project_id(ref: str) -> str:
    try:
        return project_service.resolve(ref).id
    except KeyError:
        fail(f"no project matches '{ref}'", error="not_found", ref=ref)
    except ValueError as exc:
        fail(str(exc), error="ambiguous_project", ref=ref)


def _emit_layout(message: str, entry: groups_service.UndoEntry | None = None) -> None:
    """One line, or under --json the arrangement every surface shows."""
    if get_state().json_output:
        with store_session() as store:
            arrangement = groups_service.load_arrangement(store)
        typer.echo(json.dumps(_arrangement_json(arrangement)))
        return
    stdout_console().print(message, markup=False)


def _arrangement_json(arrangement: groups_service.Arrangement) -> dict[str, object]:
    def project(p: ProjectInfo) -> dict[str, object]:
        return {"id": p.id, "name": p.root.name or p.id, "position": p.position}

    def group(e: groups_service.GroupEntry) -> dict[str, object]:
        return {
            "id": e.group.id,
            "name": e.group.name,
            "position": e.group.position,
            "collapsed": e.group.collapsed,
            "pinned": e.group.pinned_at is not None,
            "members": [project(m) for m in e.members],
        }

    return {
        "pinned": [
            group(e) if isinstance(e, groups_service.GroupEntry) else project(e)
            for e in arrangement.pinned
        ],
        "groups": [group(e) for e in arrangement.groups],
        "loose": [project(p) for p in arrangement.loose],
    }


@group_app.command("create")
def group_create(
    name: Annotated[str, typer.Argument(help="Group name (unique, case-insensitively).")],
    projects: Annotated[
        list[str] | None, typer.Argument(help="Projects to put in it (name, codename, id, path).")
    ] = None,
) -> None:
    """Create a group at the end of the list, optionally with its first members."""
    ids = [_project_id(ref) for ref in projects or []]
    try:
        with store_session() as store:
            created, _ = groups_service.create_group(store, name, ids)
    except ValueError as exc:
        fail(str(exc), error="invalid_group", ref=name)
    suffix = f" with {len(ids)} project(s)" if ids else ""
    _emit_layout(f"✓ group {created.name} created{suffix}")


@group_app.command("rename")
def group_rename(
    group: Annotated[str, typer.Argument(help="Group name or id.")],
    name: Annotated[str, typer.Argument(help="The new name.")],
) -> None:
    """Rename a group."""
    try:
        with store_session() as store:
            gid = groups_service.resolve_group(store, group).id
            groups_service.rename_group(store, gid, name)
    except KeyError:
        fail(f"no group matches '{group}'", error="not_found", ref=group)
    except ValueError as exc:
        fail(str(exc), error="invalid_group", ref=name)
    _emit_layout(f"✓ group {group} is now {name.strip()}")


@group_app.command("delete")
def group_delete(group: Annotated[str, typer.Argument(help="Group name or id.")]) -> None:
    """Delete a group; its projects go back to the top level. No project is deleted."""
    try:
        with store_session() as store:
            found = groups_service.resolve_group(store, group)
            groups_service.delete_group(store, found.id)
    except KeyError:
        fail(f"no group matches '{group}'", error="not_found", ref=group)
    _emit_layout(f"✓ group {found.name} deleted — its projects are back at the top level")


@group_app.command("list")
def group_list() -> None:
    """The groups, their order, pins and members."""
    with store_session() as store:
        arrangement = groups_service.load_arrangement(store)
    if get_state().json_output:
        typer.echo(json.dumps(_arrangement_json(arrangement)))
        return
    pinned_groups = [e for e in arrangement.pinned if isinstance(e, groups_service.GroupEntry)]
    entries = [*pinned_groups, *arrangement.groups]
    if not entries:
        stdout_console().print("No groups yet. Create one: aisquare project group create <name>")
        return
    for entry in entries:
        pin = " 📌" if entry.group.pinned_at is not None else ""
        fold = " (collapsed)" if entry.group.collapsed else ""
        names = ", ".join(m.root.name or m.id for m in entry.members) or "—"
        stdout_console().print(f"{entry.group.name}{pin}{fold}: {names}", markup=False)


@group_app.command("add")
def group_add(
    group: Annotated[str, typer.Argument(help="Group name or id.")],
    projects: Annotated[list[str], typer.Argument(help="Projects to move into it.")],
) -> None:
    """Move projects into a group (at its end, in the order given)."""
    ids = [_project_id(ref) for ref in projects]
    try:
        with store_session() as store:
            groups_service.add_to_group(store, group, ids)
    except KeyError:
        fail(f"no group matches '{group}'", error="not_found", ref=group)
    _emit_layout(f"✓ {len(ids)} project(s) moved into {group}")


@group_app.command("remove")
def group_remove(
    projects: Annotated[list[str], typer.Argument(help="Projects to take out of their group.")],
) -> None:
    """Take projects out of their group (to the end of the top level)."""
    ids = [_project_id(ref) for ref in projects]
    with store_session() as store:
        groups_service.remove_from_group(store, ids)
    _emit_layout(f"✓ {len(ids)} project(s) ungrouped")


@group_app.command("move")
def group_move(
    group: Annotated[str, typer.Argument(help="Group name or id.")],
    before: Annotated[
        str | None, typer.Option("--before", help="Put it before this group.")
    ] = None,
    after: Annotated[str | None, typer.Option("--after", help="Put it after this group.")] = None,
    position: Annotated[
        int | None, typer.Option("--position", min=0, help="Index among the groups.")
    ] = None,
) -> None:
    """Reorder a group among the groups."""
    try:
        with store_session() as store:
            gid = groups_service.resolve_group(store, group).id
            groups_service.move_group(store, gid, before=before, after=after, position=position)
    except KeyError as exc:
        fail(f"no group matches {exc.args[0]!r}", error="not_found", ref=str(exc.args[0]))
    _emit_layout(f"✓ group {group} moved")


@app.command("pin")
def pin(
    project: Annotated[str, typer.Argument(help="Project name, codename, id or path.")],
) -> None:
    """Pin a project to the top of the list (the Pinned section)."""
    pid = _project_id(project)
    with store_session() as store:
        groups_service.pin(store, pid, True)
    _emit_layout(f"✓ pinned {project}")


@app.command("unpin")
def unpin(
    project: Annotated[str, typer.Argument(help="Project name, codename, id or path.")],
) -> None:
    """Unpin a project; it returns to its group or the top level."""
    pid = _project_id(project)
    with store_session() as store:
        groups_service.pin(store, pid, False)
    _emit_layout(f"✓ unpinned {project}")


@app.command("move")
def move(
    project: Annotated[str, typer.Argument(help="Project name, codename, id or path.")],
    to: Annotated[
        str | None, typer.Option("--to", help="A group (name or id), or 'top' for no group.")
    ] = None,
    before: Annotated[
        str | None, typer.Option("--before", help="Put it before this project.")
    ] = None,
    after: Annotated[str | None, typer.Option("--after", help="Put it after this project.")] = None,
    position: Annotated[
        int | None,
        typer.Option(
            "--position", min=0, help="Index in the scope, counting the projects `list` shows."
        ),
    ] = None,
) -> None:
    """Move a project: into a group or to the top level, and to a place in that scope.

    With no place given it goes to the END of the scope, like a new tab.
    """
    pid = _project_id(project)
    try:
        with store_session() as store:
            groups_service.move_project(
                store,
                pid,
                to=to,
                before=_project_id(before) if before else None,
                after=_project_id(after) if after else None,
                position=position,
            )
    except KeyError as exc:
        fail(f"no group matches {exc.args[0]!r}", error="not_found", ref=str(exc.args[0]))
    _emit_layout(f"✓ moved {project}{_destination(to)}")


def _destination(to: str | None) -> str:
    """`` into <group>``, `` to the top level``, or nothing when the project stayed put."""
    if not to:
        return ""
    return " to the top level" if to == groups_service.TOP else f" into {to}"
