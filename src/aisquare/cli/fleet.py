"""``aisquare fleet`` — spawn, watch, steer and stop the agents of a project; and ``ui``.

Thin over :mod:`aisquare.services.fleet`: parse, call, render. Every command
honours ``--json`` (a machine-readable object on stdout, nothing else) and maps
the service's :class:`FleetError` family onto the shared ``fail`` contract, so
scripts and the manager's own ``fleet spawn`` calls read one shape.

``ui`` is what bare ``asq`` runs at a terminal (docs/plans/fleet-tui.md §3.8);
it refuses without a TTY rather than starting a full-screen app into a pipe.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Annotated, NoReturn

import typer

from aisquare.cli.common import fail
from aisquare.core.console import stdout_console
from aisquare.core.state import get_state
from aisquare.models import FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service

app = typer.Typer(
    help="The fleet: this project's manager and the agents it spawns, each a tmux pane.",
    no_args_is_help=True,
)

ProjectRef = Annotated[
    str | None,
    typer.Option(
        "--project",
        "-P",
        help="Project by codename, name or id prefix (default: the active project).",
        metavar="PROJECT",
    ),
]
SessionRef = Annotated[
    str | None,
    typer.Option("--as", help="Acting session id (a manager passes its own).", metavar="SESSION"),
]

_STATE_CHIP = {
    "working": "▶ working",
    "waiting": "⏸ waiting",
    "attention": "🔔 NEEDS YOU",
    "exited": "💤 exited",
    "lost": "✗ lost",
    "unknown": "· unknown",
}


def _fail_fleet(exc: Exception) -> NoReturn:
    """The service's reason, on stderr for a human and as ``detail`` under ``--json``."""
    if isinstance(exc, fleet_service.FleetUnavailable):
        fail(str(exc), error="fleet_unavailable", detail=str(exc))
    if isinstance(exc, fleet_service.NoSuchProject):
        fail(str(exc), error="not_found", detail=str(exc))
    if isinstance(exc, fleet_service.NoSuchAgent):
        fail(str(exc), error="no_such_agent", detail=str(exc))
    fail(str(exc), error="fleet_error", detail=str(exc))


def _project(ref: str | None) -> ProjectInfo:
    try:
        return fleet_service.resolve_project(ref)
    except fleet_service.FleetError as exc:
        _fail_fleet(exc)


def _project_json(project: ProjectInfo) -> dict[str, object]:
    return {
        "project": project.model_dump(mode="json"),
        "name": project.root.name or project.id,
        "codename": project.codename,
        "tmux_session": fleet_service.session_name(project.codename) if project.codename else None,
    }


def _agent_line(status: FleetAgentStatus) -> str:
    agent = status.agent
    chip = _STATE_CHIP.get(status.state, status.state)
    if status.state == "exited" and agent.exit_status is not None:
        chip = f"{chip}({agent.exit_status})"
    extra = f"  {status.detail}" if status.detail else ""
    where = "  (worktree)" if agent.worktree else ""
    return f"  {agent.label:<24} {agent.role:<10} {chip}{where}{extra}  {agent.pane_id}"


def _emit_agents(project: ProjectInfo, agents: list[FleetAgentStatus]) -> None:
    if get_state().json_output:
        payload = _project_json(project)
        payload["agents"] = [status.model_dump(mode="json") for status in agents]
        typer.echo(json.dumps(payload))
        return
    console = stdout_console()
    title = project.root.name or project.id
    if project.codename:
        title = f"{title} · {project.codename} · {fleet_service.session_name(project.codename)}"
    console.print(title)
    if not agents:
        console.print("  (no agents) — start one: aisquare fleet spawn manager")
        return
    for status in agents:
        console.print(_agent_line(status))


@app.command(
    "spawn",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def spawn(
    ctx: typer.Context,
    role: Annotated[
        str,
        typer.Argument(
            help=f"Role to run: {', '.join(fleet_service.FLEET_ROLES)}, or a bound one."
        ),
    ],
    label: Annotated[
        str | None,
        typer.Option("--label", "-l", help="Agent label (default: <role>-<task> or <role>-<n>)."),
    ] = None,
    task: Annotated[
        str | None, typer.Option("--task", help="Board task this agent is for (id or prefix).")
    ] = None,
    worktree: Annotated[
        bool | None,
        typer.Option("--worktree/--no-worktree", help="Run in its own git worktree."),
    ] = None,
    permission_mode: Annotated[
        str | None,
        typer.Option(
            "--permission-mode",
            help="Claude Code permission mode (auto, acceptEdits, …); default per role.",
        ),
    ] = None,
    binary: Annotated[
        str | None, typer.Option("--bin", help="Agent executable (default: the role's binding).")
    ] = None,
    prompt: Annotated[
        str | None, typer.Option("--prompt", help="First message to type once the agent is up.")
    ] = None,
    project: ProjectRef = None,
    as_session: SessionRef = None,
) -> None:
    """Start an agent in the project's fleet (a tmux window running `aisquare launch`).

    Arguments after the options are passed to the agent, as with `aisquare launch`.
    """
    target = _project(project)
    try:
        receipt = fleet_service.spawn(
            target,
            role,
            label=label,
            task_id=task,
            worktree=worktree,
            permission_mode=permission_mode,
            binary=binary,
            prompt=prompt,
            agent_args=list(ctx.args),
            spawned_by=as_session or "user",
        )
    except fleet_service.FleetError as exc:
        _fail_fleet(exc)
    if get_state().json_output:
        typer.echo(
            json.dumps(
                {
                    "agent": receipt.agent.model_dump(mode="json"),
                    "asked_label": receipt.asked_label,
                    "tmux_session": receipt.tmux_session,
                    "notes": receipt.notes,
                }
            )
        )
        return
    asked = (
        f" (asked: {receipt.asked_label})"
        if receipt.asked_label and receipt.asked_label != receipt.agent.label
        else ""
    )
    stdout_console().print(
        f"✓ spawned {receipt.agent.label}{asked} ({receipt.agent.id}) → "
        f"{receipt.tmux_session} {receipt.agent.pane_id}"
    )
    for note in receipt.notes:
        stdout_console().print(f"  ⚠ {note}")


@app.command("ls")
def ls(
    project: ProjectRef = None,
    show_all: Annotated[
        bool, typer.Option("--all", "-a", help="Include agents that have ended.")
    ] = False,
) -> None:
    """List the project's agents with their live state."""
    target = _project(project)
    try:
        agents = fleet_service.list_agents(target, live_only=not show_all)
    except fleet_service.FleetError as exc:
        _fail_fleet(exc)
    _emit_agents(target, agents)


@app.command("status")
def status(project: ProjectRef = None) -> None:
    """The project's fleet at a glance (same data as `ls`, with the session header)."""
    target = _project(project)
    try:
        agents = fleet_service.list_agents(target, live_only=True)
    except fleet_service.FleetError as exc:
        _fail_fleet(exc)
    _emit_agents(target, agents)


@app.command("tell")
def tell(
    label: Annotated[str, typer.Argument(help="Agent label, e.g. coder-auth.")],
    text: Annotated[str, typer.Argument(help="What to say.")],
    project: ProjectRef = None,
    as_session: SessionRef = None,
) -> None:
    """Type a message into a waiting agent; a busy one gets it as a board note."""
    target = _project(project)
    try:
        result = fleet_service.tell(target, label, text, sender=as_session)
    except fleet_service.FleetError as exc:
        _fail_fleet(exc)
    if get_state().json_output:
        typer.echo(json.dumps({"delivered": result.delivered, "how": result.how}))
        return
    mark = "✓" if result.delivered else "→"
    stdout_console().print(f"{mark} {label}: {result.how}")


@app.command("stop")
def stop(
    label: Annotated[str, typer.Argument(help="Agent label.")],
    project: ProjectRef = None,
    force: Annotated[bool, typer.Option("--force", help="Kill without a graceful /exit.")] = False,
) -> None:
    """Stop an agent: /exit, a grace period, then the window is killed."""
    target = _project(project)
    try:
        agent = fleet_service.stop(target, label, force=force)
    except fleet_service.FleetError as exc:
        _fail_fleet(exc)
    if get_state().json_output:
        typer.echo(json.dumps({"agent": agent.model_dump(mode="json")}))
        return
    stdout_console().print(f"✓ stopped {agent.label} ({agent.id})")


def _exec_attach(argv: list[str]) -> None:
    """Replace this process with `tmux attach` (indirection so tests can intercept)."""
    os.execvp(argv[0], argv)


@app.command("attach")
def attach(project: ProjectRef = None) -> None:
    """Attach this terminal to the project's fleet session (full-fidelity tmux)."""
    target = _project(project)
    try:
        argv = fleet_service.attach_argv(target)
    except fleet_service.FleetError as exc:
        _fail_fleet(exc)
    if get_state().json_output:
        typer.echo(json.dumps({"argv": argv}))
        return
    _exec_attach(argv)


@app.command("reap")
def reap(
    project: ProjectRef = None,
    every: Annotated[
        bool, typer.Option("--all", help="Every project's fleet, not just this one.")
    ] = False,
) -> None:
    """Record exited agents, mark vanished panes lost, remove merged worktrees."""
    target = None if every else _project(project)
    try:
        report = fleet_service.reap(target)
    except fleet_service.FleetError as exc:
        _fail_fleet(exc)
    if get_state().json_output:
        typer.echo(
            json.dumps(
                {
                    "ended": [a.model_dump(mode="json") for a in report.ended],
                    "lost": [a.model_dump(mode="json") for a in report.lost],
                    "worktrees_removed": [str(p) for p in report.worktrees_removed],
                }
            )
        )
        return
    stdout_console().print(
        f"✓ reaped: {len(report.ended)} ended, {len(report.lost)} lost, "
        f"{len(report.worktrees_removed)} worktrees removed"
    )


@app.command("rename")
def rename(
    codename: Annotated[str, typer.Argument(help="New codename, e.g. amber-otter.")],
    project: ProjectRef = None,
) -> None:
    """Set the project's fleet codename (and rename its tmux session to match)."""
    target = _project(project)
    try:
        updated = fleet_service.rename(target, codename)
    except fleet_service.FleetError as exc:
        _fail_fleet(exc)
    if get_state().json_output:
        typer.echo(json.dumps(_project_json(updated)))
        return
    stdout_console().print(
        f"✓ {updated.root.name or updated.id} is now {updated.codename} "
        f"({fleet_service.session_name(updated.codename or '')})"
    )


@app.command("pause")
def pause(project: ProjectRef = None, as_session: SessionRef = None) -> None:
    """Pause the fleet: the manager spawns nothing until `resume`."""
    target = _project(project)
    try:
        fleet_service.pause(target, session_ref=as_session)
    except fleet_service.FleetError as exc:
        _fail_fleet(exc)
    if get_state().json_output:
        typer.echo(json.dumps({"paused": True, **_project_json(target)}))
        return
    stdout_console().print(f"⏸ fleet paused for {target.root.name or target.id}")


@app.command("resume")
def resume(project: ProjectRef = None, as_session: SessionRef = None) -> None:
    """Resume a paused fleet."""
    target = _project(project)
    try:
        fleet_service.resume(target, session_ref=as_session)
    except fleet_service.FleetError as exc:
        _fail_fleet(exc)
    if get_state().json_output:
        typer.echo(json.dumps({"paused": False, **_project_json(target)}))
        return
    stdout_console().print(f"▶ fleet resumed for {target.root.name or target.id}")


def interactive_terminal() -> bool:
    """Whether a full-screen UI can run here: a TTY on both ends and a real TERM."""
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return False
    except (AttributeError, ValueError):
        return False
    return os.environ.get("TERM", "") not in ("", "dumb")


def ui() -> None:
    """Open the fleet UI — every project, agent and session in one view."""
    if get_state().json_output or not interactive_terminal():
        fail(
            "the fleet UI needs an interactive terminal (a TTY on stdin and stdout, and "
            "TERM set) — run `aisquare` in a terminal, or use `aisquare fleet ls --json`",
            error="not_a_tty",
        )
    from aisquare.cli.ui.app import run_ui  # lazy: textual is heavy and only the UI needs it

    run_ui()
