"""``aisquare fleet`` — spawn, watch, steer and stop the agents of a project; and ``ui``.

Thin over :mod:`aisquare.services.fleet`: parse, call, render. Every command
honours ``--json`` (a machine-readable object on stdout, nothing else) and maps
the service's :class:`FleetError` family onto the shared ``fail`` contract, so
scripts and the manager's own ``fleet spawn`` calls read one shape.

``ui`` is what bare ``asq`` runs at a terminal (docs/plans/fleet-tui.md §3.8);
it refuses without a TTY rather than starting a full-screen app into a pipe.

Flags are what the docs are written from, so they are uniform on purpose: every
command takes the project as ``--project/-P`` (codename, name or id prefix;
default the active one) rather than a positional, and ``--as SESSION`` names the
acting session wherever the service records who asked.
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
    """The service's reason, on stderr for a human and as ``detail`` under ``--json``.

    Four codes, most specific first — every subclass is also a ``FleetError``,
    so the order here IS the mapping: ``fleet_unavailable`` (no usable tmux),
    ``not_found`` (the project reference), ``no_such_agent`` (the label), and
    ``fleet_error`` for everything else the service refused with a reason.
    """
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


def _display_name(project: ProjectInfo) -> str:
    """Basename primary, ``project.id`` when the basename is empty (§5.7)."""
    return project.root.name or project.id


def _project_json(project: ProjectInfo) -> dict[str, object]:
    return {
        "project": project.model_dump(mode="json"),
        "name": _display_name(project),
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
    title = _display_name(project)
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

    Arguments after the options are passed to the agent, as with `aisquare launch`
    — which is also why `--json` must come BEFORE `spawn`: after the role it
    belongs to the agent.
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
    # The label the fleet ACTUALLY used comes first; the one that was asked for
    # is shown only when they differ (§5.7: a collision suffixes, never fails).
    asked = (
        f" (asked: {receipt.asked_label})"
        if receipt.asked_label and receipt.asked_label != receipt.agent.label
        else ""
    )
    console = stdout_console()
    console.print(
        f"✓ spawned {receipt.agent.label}{asked} ({receipt.agent.id}) → "
        f"{receipt.tmux_session} {receipt.agent.pane_id}"
    )
    for note in receipt.notes:
        console.print(f"  ⚠ {note}")


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
        typer.echo(json.dumps({"label": label, "delivered": result.delivered, "how": result.how}))
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
    if not interactive_terminal():
        # `tmux attach` cannot work without a terminal anyway — and refusing
        # here keeps the exec unreachable from every non-TTY harness (a test
        # sweep that reached it replaced the pytest process mid-run).
        fail(
            "fleet attach needs an interactive terminal — run it in a terminal, or use "
            "`aisquare fleet attach --json` to see the command",
            error="not_a_tty",
        )
    try:
        _exec_attach(argv)
    except OSError as exc:  # tmux vanished between the service's check and the exec
        fail(
            f"could not run {argv[0]}: {exc} — is tmux installed and on PATH?",
            error="fleet_unavailable",
            detail=str(exc),
        )


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
    console = stdout_console()
    console.print(
        f"✓ reaped: {len(report.ended)} ended, {len(report.lost)} lost, "
        f"{len(report.worktrees_removed)} worktrees removed"
    )
    # Counts alone do not tell a human WHICH agent went — name them.
    for agent in report.ended:
        code = f" (exit {agent.exit_status})" if agent.exit_status is not None else ""
        console.print(f"  💤 {agent.label}{code}")
    for agent in report.lost:
        console.print(f"  ✗ {agent.label}  pane {agent.pane_id} gone")
    for path in report.worktrees_removed:
        console.print(f"  🗑 {path}")


@app.command("rename")
def rename(
    codename: Annotated[str, typer.Argument(help="New codename, e.g. amber-otter.")],
    project: ProjectRef = None,
) -> None:
    """Set the project's fleet codename (and rename its tmux session to match)."""
    target = _project(project)
    notes: list[str] = []
    try:
        updated = fleet_service.rename(target, codename, notes=notes)
    except fleet_service.FleetError as exc:
        _fail_fleet(exc)
    if get_state().json_output:
        typer.echo(json.dumps({**_project_json(updated), "notes": notes}))
        return
    stdout_console().print(
        f"✓ {_display_name(updated)} is now {updated.codename} "
        f"({fleet_service.session_name(updated.codename or '')})"
    )
    # The rename fails OPEN on the tmux half (the row is what everything else
    # reads), and a swallowed one costs `fleet attach` — so say it here rather
    # than leave the operator to discover it at the escape hatch.
    for note in notes:
        stdout_console().print(f"  ⚠ {note}")


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
    stdout_console().print(f"⏸ fleet paused for {_display_name(target)}")


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
    stdout_console().print(f"▶ fleet resumed for {_display_name(target)}")


def not_interactive_reason() -> str | None:
    """Why a full-screen UI cannot run here, or ``None`` when it can.

    The three conditions of docs/plans/fleet-tui.md §3.8, each named so the
    refusal says which one it was — "not a TTY" is a poor answer to someone
    whose only problem is ``TERM=dumb``.
    """
    try:
        if not sys.stdin.isatty():
            return "stdin is not a TTY"
        if not sys.stdout.isatty():
            return "stdout is not a TTY"
    except (AttributeError, ValueError):  # a detached or closed stream
        return "stdin or stdout is closed"
    term = os.environ.get("TERM", "")
    if term == "":
        return "TERM is not set"
    if term == "dumb":
        return "TERM=dumb"
    return None


def interactive_terminal() -> bool:
    """Whether a full-screen UI can run here: a TTY on both ends and a real TERM."""
    return not_interactive_reason() is None


def ui() -> None:
    """Open the fleet UI — every project, agent and session in one view."""
    if get_state().json_output:
        fail(
            "the fleet UI has no --json form — for machine-readable fleet state use "
            "`aisquare fleet ls --json`",
            error="not_a_tty",
            detail="--json was given; a full-screen app has no machine-readable output",
        )
    if not interactive_terminal():
        reason = not_interactive_reason() or "not an interactive terminal"
        fail(
            f"the fleet UI needs an interactive terminal ({reason}) — run `aisquare` "
            "in a terminal, or use `aisquare fleet ls --json`",
            error="not_a_tty",
            detail=reason,
        )
    from aisquare.cli.ui.app import run_ui  # lazy: textual is heavy and only the UI needs it

    run_ui()
