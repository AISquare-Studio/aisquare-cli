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


def _stdin_is_a_terminal() -> bool:
    """Whether there is somebody to ask (indirection so tests can intercept).

    The same question ``project prune`` asks with ``sys.stdin.isatty()``; behind a
    name because ``CliRunner`` replaces ``sys.stdin`` for the duration of an
    invocation, so the confirmation branch of a destructive command would
    otherwise be unreachable from a test.
    """
    return sys.stdin.isatty()


def _row_json(row: fleet_service.ShutdownRow) -> dict[str, object]:
    """A row with the reason the SERVICE gave for it — never a cause guessed here."""
    return {"agent": row.agent.model_dump(mode="json"), "reason": row.reason}


def _emit_shutdown_plan(plan: fleet_service.ShutdownPlan) -> None:
    """What a shutdown would end, printed before anything is asked of tmux.

    Mirrors ``project prune``: the plan is the same shape under ``--json``, where
    it carries ``dry_run`` so a script cannot mistake it for a result.
    """
    if get_state().json_output:
        typer.echo(
            json.dumps(
                {
                    "dry_run": True,
                    "projects": [p.model_dump(mode="json") for p in plan.projects],
                    "agents": [a.model_dump(mode="json") for a in plan.agents],
                    "sessions": plan.sessions,
                    "absent_sockets": plan.absent_sockets,
                }
            )
        )
        return
    console = stdout_console()
    if not plan.agents and not plan.sessions:
        console.print("nothing to shut down: no live agents and no fleet tmux sessions")
        return
    console.print(
        f"about to shut down {len(plan.agents)} agent(s) "
        f"and kill {len(plan.sessions)} fleet session(s):"
    )
    names = {p.id: (p.codename or p.root.name or p.id) for p in plan.projects}
    for agent in plan.agents:
        where = names.get(agent.project_id, agent.project_id)
        if agent.tmux_socket in plan.absent_sockets:
            console.print(
                f"  ✗ {agent.label} · {where} — recorded lost "
                f"(no server answers on '{agent.tmux_socket}')"
            )
        else:
            console.print(f"  💤 {agent.label} · {where} — stopped ({agent.pane_id})")
    for session in plan.sessions:
        console.print(f"  ⌧ tmux session {session}")


def _emit_shutdown(report: fleet_service.ShutdownReport) -> None:
    """The result, with the service's reason per row and no claim it did not make."""
    if get_state().json_output:
        typer.echo(
            json.dumps(
                {
                    "stopped": [a.model_dump(mode="json") for a in report.stopped],
                    "recorded": [_row_json(row) for row in report.recorded],
                    "failed": [_row_json(row) for row in report.failed],
                    "sessions_killed": report.sessions_killed,
                    "sessions_absent": report.sessions_absent,
                    "sessions_failed": report.sessions_failed,
                    "sessions_left_up": report.sessions_left_up,
                    "servers_absent": report.servers_absent,
                    "claims_released": report.claims_released,
                    "paused_cleared": report.paused_cleared,
                    "paused_kept": report.paused_kept,
                    "incomplete_projects": report.incomplete_projects,
                }
            )
        )
        return
    console = stdout_console()
    partial = bool(report.failed or report.sessions_failed)
    console.print(
        f"{'⚠' if partial else '✓'} fleet {'PARTLY ' if partial else ''}shut down: "
        f"{len(report.stopped)} stopped, {len(report.recorded)} recorded lost, "
        f"{len(report.failed)} left live; "
        f"sessions killed: {', '.join(report.sessions_killed) or 'none'}"
    )
    for agent in report.stopped:
        # No exit status is the ordinary shape under --force (a live pane is
        # killed, and a status only ever comes from a pane that already died).
        code = f" (exit {agent.exit_status})" if agent.exit_status is not None else ""
        console.print(f"  💤 {agent.label}{code}")
    for row in report.recorded:
        console.print(f"  ✗ {row.agent.label}  recorded lost — {row.reason}")
    for row in report.failed:
        console.print(f"  ⚠ {row.agent.label}  LEFT LIVE — {row.reason}")
    for session in report.sessions_failed:
        console.print(f"  ⚠ tmux refused to kill session {session}")
    for session in report.sessions_left_up:
        console.print(f"  ⚠ session {session} left up: it holds a row left live")
    for session in report.sessions_absent:
        console.print(f"  · session {session} was already gone with its last window")
    if report.claims_released:
        console.print(
            f"  🔓 {len(report.claims_released)} claimed task(s) released back to the board"
        )
    for name in report.paused_cleared:
        console.print(f"  ▶ the fleet-paused signal on {name} was cleared")
    for name in report.paused_kept:
        console.print(f"  ⏸ {name} stays fleet-paused: it was not confirmed down")
    if partial:
        console.print(
            "  rows above marked LEFT LIVE were NOT ended: `aisquare fleet ls --all`, then "
            "stop them (or re-run this) once tmux answers"
        )
    console.print(
        "  board notes and tasks kept (claims of the rows this ended are released); "
        "the next asq / fleet spawn starts a fresh server"
    )


@app.command("shutdown")
def shutdown(
    project: ProjectRef = None,
    every: Annotated[
        bool, typer.Option("--all", help="Every project's fleet, not just this one.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Shut down without asking; required off a terminal.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Kill every agent without a graceful /exit.")
    ] = False,
) -> None:
    """Stop this project's agents, kill the fleet's sessions, record every row.

    The fleet's off switch. Unlike `tmux -L asq kill-server` by hand, the rows
    are recorded: agents on an answering server are stopped (exit status where
    tmux exposes one — `--force` kills a live pane and records none), and rows
    whose server is already gone are ended as lost, on your word, each with the
    reason. What is killed is the fleet's own `asq-<codename>` sessions, never
    the server, so nothing else on that socket goes with it; a server with
    nothing left on it exits by itself.

    This project by default, `--all` for every project. It prints what it would
    end and asks first at a terminal; off a terminal it is a dry run unless
    --yes, and under --json without --yes it prints the plan and changes nothing.
    Board notes and tasks are kept, the ended rows' claims are released, and a
    `fleet-paused` signal is cleared. Exits 1 when any row was left live.
    """
    target = None if every else _project(project)
    if not yes:
        try:
            plan = fleet_service.shutdown_plan(target)
        except fleet_service.FleetError as exc:
            _fail_fleet(exc)
        _emit_shutdown_plan(plan)
        if get_state().json_output or not (plan.agents or plan.sessions):
            return
        if not _stdin_is_a_terminal():
            stdout_console().print(
                "dry run: nothing stopped — re-run with --yes to shut the fleet down"
            )
            return
        noun = "agent" if len(plan.agents) == 1 else "agents"
        if not typer.confirm(f"Shut down {len(plan.agents)} {noun}?", default=False):
            stdout_console().print("nothing stopped")
            return
    try:
        report = fleet_service.shutdown(target, force=force)
    except fleet_service.FleetError as exc:
        _fail_fleet(exc)
    _emit_shutdown(report)
    if report.failed or report.sessions_failed:
        # The fleet is not down. Said in the report AND in the exit code, so a
        # script that only reads the code cannot mistake a partial run for one.
        raise typer.Exit(code=1)


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
