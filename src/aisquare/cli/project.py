"""``aisquare project`` (alias ``workspace``) — manage projects."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from aisquare.cli.common import (
    USAGE_ERROR,
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
from aisquare.core.state_file import StateUnwritableError
from aisquare.core.store import ContextStore, GroupSettings, store_session
from aisquare.core.workspace import find_project_root, project_id_for
from aisquare.models import FreshCounts, FreshPlan, FreshReport, ProjectGroup, ProjectInfo
from aisquare.services import fresh as fresh_service
from aisquare.services import install_route
from aisquare.services import parent as parent_service
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
        captured = [] if all else store.captured_projects()
    # Counted only when nothing is listed at all, where "nothing registered"
    # would be wrong — not when --group or --pinned filtered the list empty.
    hidden = 0 if listed else len(captured)
    projects = _matching(listed, chosen, pinned=pinned)
    filtered = None
    if listed and not projects:
        # The filter matched nothing in a list that has rows, and the empty table
        # said "No projects registered yet. Run: aisquare init" (review of #171,
        # round 1). It names the filter instead, and the step that fills it.
        where = f" in group {chosen.name}" if chosen is not None else ""
        unlisted = len(_matching(captured, chosen, pinned=pinned))
        if unlisted:
            # What it matches is captured, and the list hides it (#139): pinned or
            # grouped from a shell, or from the sidebar while `a` shows it. "pin
            # one" and "add one" named a step already taken, and taken again it
            # changed nothing (review of #171, round 2).
            noun = "directory" if unlisted == 1 else "directories"
            shown = ["aisquare", "project", "list", "--all", *(["--pinned"] if pinned else [])]
            if chosen is not None:
                shown += ["--group", chosen.name]
            filtered = (
                f"No listed {'pinned ' if pinned else ''}projects{where} — {unlisted} captured "
                f"{noun} hidden (a hooked session ran there): "
                f"{install_route.command_line(shown)}; add one: aisquare project onboard <path>"
            )
        elif pinned:
            filtered = f"No pinned projects{where} — pin one: aisquare project pin <project>"
        elif chosen is not None:
            filtered = (
                f"No projects{where} — add one: aisquare project group add "
                f"{_positional(chosen.name)} <project>"
            )
    emit_projects(
        projects,
        active_id=project_service.info().id,
        hidden=hidden,
        group_names=group_names,
        filtered=filtered,
    )


def _matching(
    projects: list[ProjectInfo], group: ProjectGroup | None, *, pinned: bool
) -> list[ProjectInfo]:
    """The rows ``--group`` and ``--pinned`` keep, in the order given."""
    if group is not None:
        projects = [p for p in projects if p.group_id == group.id]
    if pinned:
        projects = [p for p in projects if p.pinned_at is not None]
    return projects


def _positional(value: str) -> str:
    """``value`` as a shell word a command reads as an argument, never as an option.

    Quoting keeps the shell from splitting a name, not Click from reading ``-wip`` as
    options; ``--`` ends the options first (review of #171, round 2). Quoted for this shell
    (``install_route.command_line``), as every command a person is told to paste is: not by
    ``shlex.quote``, whose single quotes cmd.exe passes on (review of #259, round 20).
    """
    return install_route.command_line(["--", value] if value.startswith("-") else [value])


@app.command("switch")
def switch(name: Annotated[str, typer.Argument(help="Project name or id prefix.")]) -> None:
    """Switch the active project."""
    try:
        project = project_service.switch(name)
    except KeyError:
        fail(f"no project matches '{name}'", error="not_found", ref=name)
    except ValueError as exc:
        fail(str(exc), error="ambiguous_project", ref=name)
    except StateUnwritableError as exc:
        fail(str(exc), error="state_unwritable", ref=name)
    emit_project_action(
        f"✓ switched to {project_service.display_name(project)} ({project.id})", project
    )


@app.command("link")
def link(repo: Annotated[str, typer.Argument(help="Repository path or URL to link.")]) -> None:
    """Link another repository into the active project."""
    project = project_service.link(repo)
    emit_project_action(f"✓ linked {repo} into {project_service.display_name(project)}", project)


@app.command("rename")
def rename(
    nickname: Annotated[
        str | None,
        typer.Argument(help="What to call it: any text, up to 255 characters."),
    ] = None,
    project: Annotated[
        str | None,
        typer.Option(
            "--project",
            "-P",
            help="Project by name, codename, id prefix, nickname or path "
            "(default: the active project).",
            metavar="PROJECT",
        ),
    ] = None,
    clear: Annotated[
        bool, typer.Option("--clear", help="Drop the nickname: the folder's name shows again.")
    ] = False,
) -> None:
    """Give a project a nickname — what the sidebar and the lists call it.

    Free text, like naming a browser tab: what you are doing there. The codename
    (`fleet rename`) still names its tmux session and branches.
    """
    if nickname is None and not clear:
        fail("pass a nickname, or --clear", error="usage")
    if nickname is not None and clear:
        fail("pass a nickname or --clear, not both", error="usage")
    target = _project_ref(project) if project is not None else project_service.info()
    try:
        renamed = project_service.set_nickname(target, nickname)
    except ValueError as exc:
        fail(str(exc), error="bad_nickname", ref=target.id)
    except KeyError:
        fail(str(project_service.NotAdded(target)), error="not_found", ref=target.id)
    emit_project_action(
        f"✓ {project_service.display_name(target)} is now called "
        f"{project_service.display_name(renamed)}",
        renamed,
    )


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
    """Add the project and seed its context pool from what its files say."""
    # Looked up as `create_group` stores it: ' team ' found no group, and the create,
    # which strips, collided with `team` after the onboard (review of #203, round 3).
    name = group.strip() if group is not None else None
    if group is not None and not name:
        # Refused before the onboard, as `group create` refuses it: found by no name,
        # a blank group went to `create_group`, whose ValueError escaped uncaught after
        # the onboard had committed (review of #203, round 2).
        fail("a group needs a name", error="invalid_group", ref=group)
    try:
        report = project_service.onboard(path, refresh=refresh)
    except project_service.NotAFolder as exc:
        fail(str(exc), error="not_a_directory", ref=str(path or Path.cwd()))
    if name:
        # A folder: the onboard refused any other path.
        project_id = project_id_for(find_project_root(path or Path.cwd()))
        try:
            with store_session() as store:
                try:
                    target = groups_service.resolve_group(store, name)
                except KeyError:
                    # Made with its member in one transaction: made first and filled
                    # after, an add the store refused left an empty group behind
                    # (review of #203).
                    groups_service.create_group(store, name, [project_id])
                else:
                    groups_service.add_to_group(store, target.id, [project_id])
        except ValueError as exc:
            # Refused as `group create` refuses it (one made meanwhile by that name).
            fail(str(exc), error="invalid_group", ref=group)
    emit_onboard(report)


_PURGE_HELP = (
    "Also delete the project's context entries, prompt history, board rows, turn "
    "metrics and brain. Without it they stay in the store, hidden, and come back if the "
    "root is registered again."
)


_STALE_CAPTURE_DAYS = 30
"""How long a captured directory sits untouched before ``prune --captured-only`` takes it."""


@app.command("forget")
def forget(
    ref: Annotated[str, typer.Argument(help="Project id prefix, name, codename or path.")],
    purge: Annotated[bool, typer.Option("--purge", help=_PURGE_HELP)] = False,
) -> None:
    """Remove a project registration. Refused while it has live fleet agents, or while
    another project's fleet agents read its board (a hub's), live or stopped (a restart
    would start them under it again)."""
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
    captured = days if captured_only else None
    candidates = project_service.prune_candidates(
        missing=missing, worktrees=worktrees, captured_older_than=captured
    )
    if yes:
        emit_prune(
            project_service.prune(
                candidates,
                purge=purge,
                missing=missing,
                worktrees=worktrees,
                captured_older_than=captured,
            )
        )
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
    emit_prune(
        project_service.prune(
            candidates,
            purge=purge,
            missing=missing,
            worktrees=worktrees,
            captured_older_than=captured,
        )
    )


# --- start fresh ---------------------------------------------------------------------------


def fresh(
    project: Annotated[
        str | None,
        typer.Option(
            "--project",
            "-P",
            help="Project by name, codename, id prefix, nickname or path "
            "(default: the active project).",
            metavar="PROJECT",
        ),
    ] = None,
    memory: Annotated[
        bool,
        typer.Option(
            "--memory",
            help="Also clear its remembered context: context entries and the project brain.",
        ),
    ] = False,
    prompts: Annotated[
        bool, typer.Option("--prompts", help="Also clear its prompt log and turn metrics.")
    ] = False,
    branches: Annotated[
        bool,
        typer.Option(
            "--branches",
            help="Also delete the local fleet/<codename>/… branches its agents made (never a "
            "remote one).",
        ),
    ] = False,
    discard_unpushed: Annotated[
        bool,
        typer.Option(
            "--discard-unpushed",
            help="Throw away what its agents' worktrees and branches hold that no remote has; "
            "without it such work refuses the reset. At a terminal only what the plan listed "
            "and you confirmed goes (anything found since refuses); with --yes, whatever the "
            "run finds.",
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Start fresh without asking; required off a terminal."),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print what it would clear and change nothing.")
    ] = False,
) -> None:
    """Start fresh: clear the project's board and fleet, then start a blank manager (alias: reset).

    Every live agent is stopped as `fleet shutdown` stops it, each agent's worktree
    is removed, and the board's tasks, notes, sessions, signals and work briefs (with
    their kept evidence), the manager's state file and the fleet's rows are deleted;
    task and brief numbers start at #1 again. The checkout, every other branch, the
    registration and its settings, the symbol index and every other project are kept,
    and so are its context entries and brain, prompt log and fleet branches unless
    --memory, --prompts or --branches says otherwise. No command report is deleted,
    but with their briefs gone they are no longer pinned as evidence (unless another
    board's brief cites one), and normal retention takes them. Work its worktrees or
    branches hold that no remote has refuses the reset, named, unless
    --discard-unpushed. It prints the plan and asks first at a terminal (twice when
    discarding); off a terminal it is a dry run unless --yes, and under --json
    without --yes it prints the plan and changes nothing. Refused from inside one
    of the project's own fleet agents, and while another project's fleet agents
    read its board.
    """
    target = _project_ref(project) if project is not None else project_service.info()
    try:
        planned = fresh_service.plan(target, memory=memory, prompts=prompts, branches=branches)
    except fresh_service.FreshRefused as exc:
        fail(str(exc), error=exc.code, ref=target.id, detail=str(exc), exit_code=2)
    if dry_run or (get_state().json_output and not yes):
        _emit_fresh_plan(planned, discard=discard_unpushed)
        return
    if planned.unpushed and not discard_unpushed:
        fail(
            fresh_service.unpushed_refusal(planned),
            error="unpushed_work",
            ref=target.id,
            detail="; ".join(fresh_service.unpushed_line(item) for item in planned.unpushed),
            exit_code=2,
        )
    if not yes:
        _emit_fresh_plan(planned, discard=discard_unpushed)
        if not _stdin_is_a_terminal():
            stdout_console().print("dry run: nothing cleared — re-run with --yes to start fresh")
            return
        name = project_service.display_name(planned.project)
        if not typer.confirm(f"Start {name} fresh?", default=False):
            stdout_console().print("nothing cleared")
            return
        if planned.unpushed and not typer.confirm(
            f"Throw away the unpushed work in {len(planned.unpushed)} "
            f"place{'s' if len(planned.unpushed) != 1 else ''}, as listed above "
            "(uncommitted changes go as they are when the reset runs)?",
            default=False,
        ):
            stdout_console().print("nothing cleared")
            return
    from aisquare.services import fleet as fleet_service  # lazy: the fleet is heavy to import

    # Asked at a terminal, the yes covers the list it was asked about, at the tips it showed;
    # with --yes nothing was shown, and the flag is the whole of the consent.
    discard: fresh_service.Discard = ()
    if discard_unpushed:
        discard = "all" if yes else list(planned.unpushed)
    try:
        report = fresh_service.start_fresh(
            planned.project,
            memory=memory,
            prompts=prompts,
            branches=branches,
            discard=discard,
            actor=fleet_service.acting_as(),
            ack_wait=fleet_service.settings().prompt_ack_seconds,
        )
    except fresh_service.FreshRefused as exc:
        said = str(exc)
        if exc.code == "unpushed_work" and exc.plan is not None and discard_unpushed:
            # Work found since the list confirmed at the terminal, before anything was
            # stopped: the flag they passed, named again, would only ask about the same list.
            said = fresh_service.unpushed_refusal(exc.plan, way=fresh_service.RUN_AGAIN)
        fail(said, error=exc.code, ref=target.id, detail=said, exit_code=2)
    except fresh_service.FreshIncomplete as exc:
        said = str(exc)
        if exc.plan is not None and discard_unpushed:
            # The list confirmed at the terminal did not cover what the stop left: naming
            # the flag again would only ask about the same list.
            said = fresh_service.found_after_stop(exc.plan, way=fresh_service.RUN_AGAIN)
        if get_state().json_output:
            # The partial report too, under the error: what was stopped and removed before
            # it stood still, which a script cannot read off a bare error object.
            payload: dict[str, object] = {
                "error": "fresh_incomplete",
                "ref": target.id,
                "detail": said,
                "report": exc.report.model_dump(mode="json"),
            }
            if exc.plan is not None:  # work found once the fleet was stopped: what it is
                payload["plan"] = exc.plan.model_dump(mode="json")
            typer.echo(json.dumps(payload))
            raise typer.Exit(code=1) from None
        _emit_fresh(exc.report)
        fail(said, error="fresh_incomplete", ref=target.id)
    _emit_fresh(report)
    if report.manager_error is not None:
        raise typer.Exit(code=1)


app.command("fresh")(fresh)
app.command("reset", hidden=True, help="Alias of 'fresh'.")(fresh)


def _stdin_is_a_terminal() -> bool:
    """Whether there is somebody to ask, as ``fleet shutdown`` asks it: behind a name because
    ``CliRunner`` replaces ``sys.stdin`` for an invocation, and the confirmations of a
    destructive command would be out of a test's reach otherwise."""
    return sys.stdin.isatty()


def _emit_fresh_plan(plan: FreshPlan, *, discard: bool) -> None:
    """``project fresh``'s plan — as JSON under ``--json``, lines otherwise."""
    if get_state().json_output:
        typer.echo(plan.model_dump_json())
        return
    console = stdout_console()
    project = plan.project
    console.print(
        f"Start fresh: {project_service.display_name(project)} ({project.codename or project.id}) "
        f"at {project.root}",
        markup=False,
    )
    console.print(f"  clears  {' · '.join(fresh_service.count_words(plan.counts))}", markup=False)
    if plan.live_agents:
        console.print(f"  stops   {', '.join(plan.live_agents)} first", markup=False)
    if plan.sessions:
        console.print(f"  kills   tmux session {', '.join(plan.sessions)}", markup=False)
    for path in plan.worktrees:
        console.print(f"  🗑 {path}", markup=False)
    for branch in plan.fleet_branches:
        console.print(f"  🗑 branch {branch}", markup=False)
    kept = [
        what
        for what, chosen in (
            ("context entries and brain (--memory)", plan.memory),
            ("prompt log and metrics (--prompts)", plan.prompts),
            ("fleet branches (--branches)", plan.branches),
        )
        if not chosen
    ]
    if kept:
        console.print(f"  keeps   {', '.join(kept)}", markup=False)
    console.print("  then    a new manager starts on the empty board", markup=False)
    if plan.unpushed:
        said = (
            "thrown away (--discard-unpushed)" if discard else "refused without --discard-unpushed"
        )
        console.print(f"  ⚠ work found nowhere else — {said}:", markup=False)
        for item in plan.unpushed:
            console.print(f"    {fresh_service.unpushed_line(item)}", markup=False)


def _emit_fresh(report: FreshReport) -> None:
    """``project fresh``'s report — as JSON under ``--json``, lines otherwise."""
    if get_state().json_output:
        typer.echo(report.model_dump_json())
        return
    console = stdout_console()
    project = report.project
    name = project_service.display_name(project)
    if report.cleared != FreshCounts():
        cleared = ", ".join(fresh_service.cleared_words(report))
        console.print(f"✓ started {name} fresh — cleared {cleared}", markup=False)
    if report.stopped:
        console.print(f"  stopped: {', '.join(report.stopped)}", markup=False)
    ended = [*report.sessions_killed, *report.sessions_gone]
    if ended:
        console.print(f"  tmux session {', '.join(ended)} ended", markup=False)
    for path in report.worktrees_removed:
        console.print(f"  🗑 removed worktree {path}", markup=False)
    for branch in report.branches_deleted:
        console.print(f"  🗑 deleted branch {branch}", markup=False)
    for session in report.sessions_failed:
        console.print(f"  ⚠ tmux would not kill {session}", markup=False)
    for item in report.discarded:
        console.print(f"  ⚠ threw away {fresh_service.unpushed_line(item)}", markup=False)
    for kept in report.kept:
        where = str(kept.path) if kept.path is not None else f"branch {kept.branch}"
        console.print(f"  ⚠ kept {where} — {kept.reason}", markup=False)
    if report.manager is not None:
        session = (report.manager.session_id or "")[:8]
        console.print(
            f"  ✓ a new manager started: {report.manager.id} · session {session} "
            f"· pane {report.manager.pane_id}",
            markup=False,
        )
    elif report.manager_error is not None:
        spawn = project_service.fleet_command("spawn", project, "manager")
        console.print(
            f"  ✗ no manager started: {report.manager_error} — start one: {spawn}", markup=False
        )


# --- groups, pins and manual order (#140) ---------------------------------------------------

group_app = typer.Typer(
    help="Project groups: a named, collapsible container for listing and managing — "
    "nothing inside a project is shared.",
    no_args_is_help=True,
)
app.add_typer(group_app, name="group")


def _project_id(ref: str) -> str:
    return _project_ref(ref).id


def _project_ref(ref: str) -> ProjectInfo:
    try:
        return project_service.resolve(ref)
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
        return {"id": p.id, "name": project_service.display_name(p), "position": p.position}

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
        names = ", ".join(project_service.display_name(m) for m in entry.members) or "—"
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


def _emit_parent(group: str, settings: GroupSettings, line: str) -> None:
    """One line, or under --json the group's parent settings (P2-W6)."""
    if get_state().json_output:
        typer.echo(
            json.dumps(
                {
                    "group": group,
                    "parent_project_id": settings.parent_project_id,
                    "route": settings.route,
                    "max_agents": settings.max_agents,
                }
            )
        )
        return
    stdout_console().print(line, markup=False)


def _group_id(store: ContextStore, group: str) -> str:
    try:
        return groups_service.resolve_group(store, group).id
    except KeyError:
        fail(f"no group matches '{group}'", error="not_found", ref=group)


@group_app.command("parent")
def group_parent(
    ctx: typer.Context,
    group: Annotated[str, typer.Argument(help="Group name or id.")],
    project: Annotated[
        str | None,
        typer.Argument(help="The parent: a project outside the group, its coordination home."),
    ] = None,
    clear: Annotated[bool, typer.Option("--clear", help="The group has no parent.")] = False,
) -> None:
    """Name a group's parent: its manager hears the members' questions, blocks, results and
    linked tasks (`project group route` chooses) as `routed` lines on its board, nudged."""
    if project is not None and clear:
        fail("give a project or --clear, not both", error="usage", exit_code=2)
    if project is None and not clear:
        # The words typer says for a missing required argument, through its own usage error
        # (as `fleet stop` does for its label): exit 2, and JSON under --json.
        raise USAGE_ERROR("Missing argument 'project'.", ctx)
    parent = _project_ref(project) if project is not None else None
    with store_session() as store:
        gid = _group_id(store, group)
        if parent is not None and parent.group_id == gid:
            fail(
                f"{project} is a member of {group}: a parent is a project outside it",
                error="invalid_group",
                ref=project,
            )
        settings = store.set_group_settings(
            gid, parent_project_id=parent.id if parent is not None else None
        )
    line = (
        f"✓ {group}'s parent is {project_service.display_name(parent)}"
        if parent is not None
        else f"✓ {group} has no parent"
    )
    _emit_parent(group, settings, line)


@group_app.command("route")
def group_route(
    group: Annotated[str, typer.Argument(help="Group name or id.")],
    kinds: Annotated[
        str | None,
        typer.Option(
            "--kinds",
            help="Comma-separated: question, task_blocked, result, linked (the default), "
            "task_done, task_review, task_reopened, note.",
        ),
    ] = None,
    pattern: Annotated[
        str | None,
        typer.Option(
            "--pattern", help="A regex: a note whose text matches routes too ('' clears)."
        ),
    ] = None,
    off: Annotated[bool, typer.Option("--off", help="Route nothing up.")] = False,
) -> None:
    """Choose what a group's members route up to its parent's manager."""
    with store_session() as store:
        gid = _group_id(store, group)
        current = store.group_settings(gid)
        try:
            route = parent_service.route_settings(
                current.route,
                kinds=[k.strip() for k in kinds.split(",") if k.strip()] if kinds else None,
                pattern=pattern,
                off=off,
            )
        except ValueError as exc:
            fail(str(exc), error="invalid_route", ref=group)
        settings = store.set_group_settings(gid, route=route)
    said = "off" if off else ", ".join(route.get("kinds") or parent_service.DEFAULT_KINDS)
    if route.get("pattern") and not off:
        said += f"; notes matching {route['pattern']!r}"
    _emit_parent(group, settings, f"✓ {group} routes up: {said}")


@group_app.command("cap")
def group_cap(
    group: Annotated[str, typer.Argument(help="Group name or id.")],
    max_agents: Annotated[
        int, typer.Argument(min=0, help="Live agents across its members and parent; 0: no cap.")
    ],
) -> None:
    """Cap the live agents across a group's members and its parent (checked at spawn)."""
    with store_session() as store:
        gid = _group_id(store, group)
        settings = store.set_group_settings(gid, max_agents=max_agents)
    _emit_parent(
        group,
        settings,
        f"✓ {group}: at most {max_agents} live agents" if max_agents else f"✓ {group}: no cap",
    )


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
