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
from collections.abc import Mapping
from typing import Annotated, NoReturn

import typer
from rich.console import Console

from aisquare.cli.common import USAGE_ERROR, fail, refuse_conflicting_scope
from aisquare.cli.native_args import NativeForwardingCommand
from aisquare.core.console import stdout_console
from aisquare.core.state import get_state
from aisquare.models import FleetAgentStatus, ProjectInfo
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import fleet as fleet_service
from aisquare.services import install_route
from aisquare.services import project as project_service

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
EveryAgent = Annotated[
    bool,
    typer.Option(
        "--all",
        help="Every running agent in every project's fleet, not one label (not with a label "
        "or --project). A restart also brings back every agent that died (killed, crashed, "
        "its server gone) and leaves one stopped on purpose; a stop leaves exited (💤) agents "
        "as they are. The agent running this command is left either way.",
    ),
]
ManagersOnly = Annotated[
    bool, typer.Option("--managers", help="With --all: the managers alone, not their teams.")
]

_STATE_CHIP = {
    "working": "▶ working",
    "waiting": "⏸ waiting",
    "attention": "🔔 NEEDS YOU",
    "limited": "⏳ limited",
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
    """``services.project.display_name``: the nickname, the basename, else ``project.id``
    (§5.7). Free text: a line that prints it passes ``markup=False``."""
    return project_service.display_name(project)


def _project_json(project: ProjectInfo) -> dict[str, object]:
    return {
        "project": project.model_dump(mode="json"),
        "name": _display_name(project),
        "codename": project.codename,
        "tmux_session": fleet_service.session_name(project.codename) if project.codename else None,
    }


def _agent_line(status: FleetAgentStatus, labels: Mapping[int, str] | None = None) -> str:
    agent = status.agent
    chip = _STATE_CHIP.get(status.state, status.state)
    if status.state == "exited" and agent.exit_status is not None:
        chip = f"{chip}({agent.exit_status})"
    extra = f"  {status.detail}" if status.detail else ""
    where = "  (worktree)" if agent.worktree else ""
    # The slot the spawn resolved to (#145), so a row says which limit pool it
    # draws on — under the label the launch line, the feed and the Accounts
    # page use (the alias, or `plain claude` for slot 1; review of #205, finding
    # 10); absent when nothing chose and the window ran on its shell's claude.
    on = f"  {slot_label(agent.account_slot, labels)}" if agent.account_slot is not None else ""
    persona = f"  · {agent.persona}" if agent.persona else ""
    return (
        f"  {agent.label:<24} {agent.role:<10} {chip}{where}{on}{persona}{extra}  {agent.pane_id}"
    )


def slot_label(slot: int, labels: Mapping[int, str] | None) -> str:
    """``labels[slot]`` (``services.claude_accounts.slot_labels``), else the built-in name."""
    named = (labels or {}).get(slot)
    if named:
        return named
    from aisquare.core.claude_accounts import slot_name

    return slot_name(slot)


def agents_json(project: ProjectInfo, agents: list[FleetAgentStatus]) -> dict[str, object]:
    """The ``fleet ls --json`` payload — shared with the Remote Control server verbatim."""
    payload = _project_json(project)
    payload["agents"] = [status.model_dump(mode="json") for status in agents]
    return payload


def _emit_agents(project: ProjectInfo, agents: list[FleetAgentStatus]) -> None:
    if get_state().json_output:
        typer.echo(json.dumps(agents_json(project, agents)))
        return
    console = stdout_console()
    title = _display_name(project)
    if project.codename:
        title = f"{title} · {project.codename} · {fleet_service.session_name(project.codename)}"
    console.print(title, markup=False)
    if not agents:
        spawn = project_service.fleet_command("spawn", project, "manager")
        console.print(f"  (no agents) — start one: {spawn}", markup=False)
        return
    labels = accounts_service.slot_labels() if any(s.agent.account_slot for s in agents) else {}
    for status in agents:
        console.print(_agent_line(status, labels))


@app.command(
    "spawn",
    cls=NativeForwardingCommand,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def spawn(
    ctx: typer.Context,
    role: Annotated[
        str,
        typer.Argument(
            help=f"Role to run: {', '.join(fleet_service.FLEET_ROLES)}, a bound one, or "
            f"{fleet_service.PLAIN_ROLE} for none (a plain session of its agent)."
        ),
    ],
    label: Annotated[
        str | None,
        typer.Option("--label", "-l", help="Agent label (default: <role>-<task> or <role>-<n>)."),
    ] = None,
    task: Annotated[
        str | None,
        typer.Option("--task", help="Board task this agent is for (#12, 12, or id prefix)."),
    ] = None,
    worktree: Annotated[
        bool | None,
        typer.Option("--worktree/--no-worktree", help="Run in its own git worktree."),
    ] = None,
    permission_mode: Annotated[
        str | None,
        typer.Option(
            "--permission-mode",
            help="Permission as WRITE:APPROVALS, mapped to each agent's own flags "
            "(workspace:auto, read_only:auto, workspace:ask, full:never…; docs/fleet.md), "
            "or in the agent's own words (Claude Code: auto, acceptEdits, …; Codex: a "
            "sandbox); default per role.",
        ),
    ] = None,
    agent: Annotated[str | None, typer.Option("--agent", help="Coding agent family.")] = None,
    sandbox: Annotated[
        str | None,
        typer.Option(
            "--sandbox", help="Codex write scope: read-only, workspace-write, danger-full-access."
        ),
    ] = None,
    approval: Annotated[
        str | None,
        typer.Option("--approval", help="Codex approval policy: on-request, untrusted, never."),
    ] = None,
    binary: Annotated[
        str | None, typer.Option("--bin", help="Agent executable (default: the role's binding).")
    ] = None,
    prompt: Annotated[
        str | None,
        typer.Option(
            "--prompt",
            help="First message for the agent: on Claude Code's command line, typed for any "
            "other binary. With --task it follows the task's kickoff.",
        ),
    ] = None,
    account: Annotated[
        str | None,
        typer.Option(
            "--account",
            help="Account to run under, one of the agent's own (Claude Code or Codex): a "
            "slot number, alias or email (see `aisquare accounts`); default: the role's "
            "binding, the project default, then the machine default.",
            metavar="ACCOUNT",
        ),
    ] = None,
    persona: Annotated[
        str | None,
        typer.Option(
            "--persona",
            help="Persona the agent runs as (see `aisquare persona list`); default: the "
            "role's fleet.roles.<role>.persona setting, else none.",
            metavar="NAME",
        ),
    ] = None,
    board_context: Annotated[
        str | None,
        typer.Option(
            "--board-context",
            help="What of the board it reads: all, relevant (its tasks, what is addressed "
            "to it, decisions) or none; default: the role's "
            "fleet.roles.<role>.board_context (reviewer: none). Kept by restart and switch.",
            metavar="all|relevant|none",
        ),
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
            sandbox=sandbox,
            approval_policy=approval,
            binary=binary,
            agent=agent,
            prompt=prompt,
            agent_args=list(ctx.args),
            spawned_by=as_session or "user",
            account=account,
            persona=persona,
            ack_wait=fleet_service.settings().prompt_ack_seconds,
            board_context=board_context,
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
                    **_first_prompt_json(receipt),
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
    persona = f" · persona {receipt.agent.persona}" if receipt.agent.persona else ""
    console = stdout_console()
    console.print(
        f"✓ spawned {receipt.agent.label}{asked} ({receipt.agent.id}) → "
        f"{receipt.tmux_session} {receipt.agent.pane_id}{persona}"
    )
    if receipt.prompt_acked:
        console.print(f"  ✓ {_first_prompt_line(receipt.prompt_delivery)}")
    for note in receipt.notes:
        console.print(f"  {'✓' if note in receipt.shared else '⚠'} {note}")


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


@app.command("restart")
def restart(
    ctx: typer.Context,
    label: Annotated[
        str | None,
        typer.Argument(help="Agent label, e.g. manager or coder-auth (not with --all)."),
    ] = None,
    fresh: Annotated[
        bool,
        typer.Option(
            "--fresh",
            help="Start a new session with a hand-off prompt built from the board instead of "
            "resuming the agent's transcript.",
        ),
    ] = False,
    permission_mode: Annotated[
        str | None,
        typer.Option(
            "--permission-mode",
            help="Permission for the replacement: WRITE:APPROVALS mapped per agent "
            "(workspace:auto, read_only:auto…) or the agent's own (Claude Code: auto, "
            "acceptEdits, …); default: the one the agent was launched with, or the role's "
            "for an agent spawned before that was recorded (#144).",
        ),
    ] = None,
    every: EveryAgent = False,
    managers: ManagersOnly = False,
    project: ProjectRef = None,
    as_session: SessionRef = None,
) -> None:
    """Start an agent again under its own label — exited, lost or still running.

    Same role, task, worktree and account; the session is resumed from its
    transcript when that file is on disk (`claude --resume <transcript>`), so a
    manager killed with ctrl+c comes back knowing its intake and its coders.
    A running agent is stopped first and handed over as `fleet switch` hands
    one over: its claims wait for the replacement and no exit is announced.
    The replacement is launched as the agent was (#144) — binary, permission
    mode, arguments — not as the role's config reads today; --permission-mode
    changes the mode, and later restarts keep it.

    With --all, every running agent in every project is restarted so, and so is
    every one that died without being stopped (killed, crashed, its tmux server
    gone); one stopped on purpose is left. One line each by project, then a
    count of running and dead; --managers restarts the managers alone, and a
    restarted manager is told which of its team died and the command that
    brings each back. It exits 1 when any of them failed, and 130 when
    interrupted, after saying how far it got.
    """
    label = _one_or_every(
        ctx,
        label,
        every,
        managers,
        project,
        {"--fresh": fresh, "--permission-mode": permission_mode},
    )
    if label is None:
        _bulk("restart", managers_only=managers, acting_session=as_session)
        return
    target = _project(project)
    try:
        receipt = fleet_service.restart(
            target,
            label,
            fresh=fresh,
            spawned_by=as_session or "user",
            permission_mode=permission_mode,
            ack_wait=fleet_service.settings().prompt_ack_seconds,
        )
    except fleet_service.FleetError as exc:
        _fail_fleet(exc)
    if get_state().json_output:
        typer.echo(json.dumps(_restart_json(receipt)))
        return
    console = stdout_console()
    before = "stopped and " if receipt.was_running else ""
    console.print(
        f"✓ {label}: {before}restarted — {receipt.how} "
        f"({receipt.started.pane_id} in {receipt.tmux_session})",
        markup=False,
    )
    for note in receipt.notes:
        console.print(f"  · {note}", markup=False)


@app.command("switch")
def switch(
    label: Annotated[str, typer.Argument(help="Agent label, e.g. coder-auth.")],
    to: Annotated[
        str | None,
        typer.Option(
            "--to",
            help="Account to move it to: a slot, alias or email. Default: the account with "
            "the most headroom that is not the one it is on.",
            metavar="ACCOUNT",
        ),
    ] = None,
    fresh: Annotated[
        bool,
        typer.Option(
            "--fresh",
            help="Start a new session with a hand-off prompt built from the board instead of "
            "resuming the agent's transcript.",
        ),
    ] = False,
    reason: Annotated[
        str | None,
        typer.Option("--reason", help="Why (recorded on the board), e.g. 'session limit'."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Move an agent the usage-limit hand-over moved minutes ago (refused "
            "without it: one switch per limit).",
        ),
    ] = False,
    project: ProjectRef = None,
    as_session: SessionRef = None,
) -> None:
    """Move a running agent to another Claude account — same label, role, task and worktree.

    The agent is stopped as `fleet stop` stops it and started again on the
    target account. With its transcript on disk the replacement resumes the
    same session (`claude --resume <transcript>`); otherwise, or with --fresh,
    it starts new with a hand-off prompt from the board. The manager runs this
    with `--as <its session>` when the usage-limit hand-over could not
    (`handover_failed`). An agent that hand-over moved minutes ago is refused
    without --force: one switch per limit.
    """
    target = _project(project)
    try:
        receipt = fleet_service.switch(
            target,
            label,
            to=to,
            fresh=fresh,
            reason=reason,
            spawned_by=as_session or "user",
            ack_wait=fleet_service.settings().prompt_ack_seconds,
            force=force,
        )
    except fleet_service.FleetError as exc:
        _fail_fleet(exc)
    if get_state().json_output:
        typer.echo(
            json.dumps(
                {
                    "stopped": receipt.stopped.model_dump(mode="json"),
                    "started": receipt.started.model_dump(mode="json"),
                    "from_slot": receipt.from_slot,
                    "to_slot": receipt.to_slot,
                    "resumed": receipt.resumed,
                    "prompt_typed": fleet_service.prompt_typed_json(receipt.prompt_typed),
                    "prompt_delivery": receipt.prompt_delivery,
                    "prompt_acked": receipt.prompt_acked,
                    "how": receipt.how,
                    "failures": receipt.failures,
                    "tmux_session": receipt.tmux_session,
                    "notes": receipt.notes,
                }
            )
        )
        return
    console = stdout_console()
    # By name, as the feed's `switched` line and `fleet ls` say it, with the slot beside it.
    labels = accounts_service.slot_labels()
    origin = (
        f"{slot_label(receipt.from_slot, labels)} (slot {receipt.from_slot})"
        if receipt.from_slot is not None
        else "its shell's claude"
    )
    target_name = f"{slot_label(receipt.to_slot, labels)} (slot {receipt.to_slot})"
    console.print(
        f"✓ {label}: moved from {origin} to {target_name} — {receipt.how} "
        f"({receipt.started.pane_id} in {receipt.tmux_session})",
        markup=False,
    )
    for note in receipt.notes:
        console.print(f"  · {note}", markup=False)


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
        # `typed` tells "may still arrive" (`maybe`) from "not typed" (`no`), which `delivered`
        # alone reads the same: a caller retrying on `false` typed it twice (review of #259,
        # round 11, 9/13).
        told = {"label": label, "delivered": result.delivered, "typed": result.typed}
        typer.echo(json.dumps({**told, "how": result.how}))
        return
    # `→` is a note filed; text at its prompt unsent, or on its way, is neither (round 12, 2/9).
    mark = {"yes": "✓", "no": "→"}.get(result.typed, "…")
    stdout_console().print(f"{mark} {label}: {result.how}")


@app.command("stop")
def stop(
    ctx: typer.Context,
    label: Annotated[str | None, typer.Argument(help="Agent label (not with --all).")] = None,
    project: ProjectRef = None,
    force: Annotated[bool, typer.Option("--force", help="Kill without a graceful /exit.")] = False,
    every: EveryAgent = False,
    managers: ManagersOnly = False,
    as_session: SessionRef = None,
) -> None:
    """Stop an agent: /exit, a grace period, then the window is killed.

    On an agent that already exited (💤) it removes the dead window that
    `remain-on-exit` kept, which is what takes the row off the live listing.

    The board's `agent_exited` names who stopped it: --as SESSION, else the session of
    the fleet window you run it in, else `user`. A manager's own stop wakes no one.

    With --all, every running agent in every project is stopped so, one line
    each by project, then a count; --managers stops the managers alone. It exits
    1 when any of them failed, and 130 when interrupted, after saying how far it
    got.
    """
    label = _one_or_every(ctx, label, every, managers, project, {"--force": force})
    if label is None:
        _bulk("stop", managers_only=managers, acting_session=as_session)
        return
    target = _project(project)
    try:
        receipt = fleet_service.stop(
            target, label, force=force, actor=fleet_service.acting_as(as_session)
        )
    except fleet_service.FleetError as exc:
        _fail_fleet(exc)
    agent = receipt.agent
    if get_state().json_output:
        typer.echo(json.dumps(_stop_json(receipt)))
    else:
        console = stdout_console()
        if receipt.marked_only:
            console.print(
                f"✓ marked {agent.label} ({agent.id}) stopped — it had already ended and no "
                "window of its was left; Restart all leaves it"
            )
        else:
            console.print(f"✓ stopped {agent.label} ({agent.id})")
        for line in _stop_notes(receipt, target):
            console.print(f"  {line}", markup=False)
    if receipt.release_failed:
        # One contract with `shutdown`: a claim left with a session that no
        # longer exists is not a clean stop, whichever command produced it, and
        # a script gating on the code must not read it as one (round 5).
        raise typer.Exit(code=1)


def _one_or_every(
    ctx: typer.Context,
    label: str | None,
    every: bool,
    managers: bool,
    project: str | None,
    single: Mapping[str, object],
) -> str | None:
    """The label ``restart`` or ``stop`` acts on, or ``None`` for ``--all``; the usage errors.

    ``--all`` is every project, so it takes neither a label nor ``--project`` (as ``reap
    --all`` does not), nor the flags ``single`` names, which are for one agent. ``--managers``
    narrows ``--all`` and means nothing without it. With neither, a label is required, as it
    always was.
    """
    if managers and not every:
        raise typer.BadParameter(
            "--managers goes with --all: it narrows every project's agents to the managers",
            param_hint="--managers",
        )
    if every and label is not None:
        raise typer.BadParameter(
            f"--all and a label conflict: --all is every running agent, {label!r} one agent — "
            "drop one of them",
            param_hint="--all",
        )
    refuse_conflicting_scope(every, project)
    for flag, value in single.items():
        if every and value not in (None, False):
            raise typer.BadParameter(
                f"{flag} is for one agent and does not go with --all", param_hint=flag
            )
    if not every and label is None:
        # The words typer said when the label was a required argument, through the usage
        # error typer itself raises (its click is vendored): exit 2, and JSON under --json.
        raise USAGE_ERROR("Missing argument 'label'.", ctx)
    return label


def _bulk(
    action: fleet_service.BulkAction, *, managers_only: bool, acting_session: str | None
) -> None:
    """``restart --all`` / ``stop --all``: :func:`fleet_service.bulk`, printed as it goes.

    Each agent's line is printed when it is done with, under its project's name, so a long
    restart shows how far it got; the count follows. Exits 1 when anything failed
    (``BulkReport.ok``), as ``stop`` and ``reap`` do, and 130 when interrupted, with the
    report of how far it got (``--json`` included), as ``shutdown`` does.
    """
    as_json = get_state().json_output
    console = stdout_console()
    heading: list[str] = []

    def said(receipt: fleet_service.BulkReceipt) -> None:
        if heading[-1:] != [receipt.project.id]:
            heading.append(receipt.project.id)
            console.print(_display_name(receipt.project), style="bold", markup=False)
        for line in _bulk_lines(receipt):
            console.print(line, markup=False)

    try:
        report = fleet_service.bulk(
            action,
            managers_only=managers_only,
            acting_session=acting_session,
            on_receipt=None if as_json else said,
        )
    except fleet_service.BulkInterrupted as exc:
        # The operator's Ctrl-C: the agents before it were restarted or stopped, so they
        # are told how far it got, and the code says it was not every agent.
        _emit_bulk(exc.report, console, as_json=as_json)
        raise typer.Exit(code=130) from None
    except fleet_service.FleetError as exc:
        _fail_fleet(exc)
    _emit_bulk(report, console, as_json=as_json)
    if not report.ok:
        raise typer.Exit(code=1)


def _emit_bulk(report: fleet_service.BulkReport, console: Console, *, as_json: bool) -> None:
    """What ``--all`` says once its agents are done with, or once it was interrupted: the
    whole report under ``--json``, else what the agents' own lines have not said."""
    if as_json:
        typer.echo(json.dumps(_bulk_json(report)))
        return
    for unread, why in report.unread:
        console.print(
            f"⚠ {_display_name(unread)}: its agents could not be read — {why}", markup=False
        )
    if report.receipts or report.unread or report.interrupted:
        console.print(report.summary())
    else:
        who = "manager" if report.managers_only else "agent"
        console.print(f"nothing to {report.action}: no {who} is running in any project")
    if report.interrupted:
        console.print(f"⚠ {report.interrupted}", markup=False)
    released = sum(
        len(receipt.result.released)
        for receipt in report.receipts
        if isinstance(receipt.result, fleet_service.StopReceipt)
    )
    _say_released(console, released)


def _stop_notes(receipt: fleet_service.StopReceipt, project: ProjectInfo) -> list[str]:
    """What a stop leaves the operator, under its line: ``fleet stop``'s, and each agent's of
    ``stop --all``, which said neither until review of #266, round 3.

    The reap that removes the worktree it left on disk, and the work it returned to the
    pool, which is the one thing the next agent inherits from this one: here the receipt
    carries the tasks, so they are named under the one line every fleet command uses for
    the count (:func:`_released_line`). Then the claims it could not give back.
    """
    notes: list[str] = []
    if receipt.killed:
        notes.append(f"⚠ {receipt.killed}")
    if receipt.agent.worktree:
        reap = project_service.fleet_command("reap", project)
        notes.append(f"· run `{reap}` to remove its worktree once its work is pushed")
    if receipt.released:
        notes.append(_released_line(len(receipt.released)))
        notes.extend(f"   · {task.title} ({task.id})" for task in receipt.released)
    if receipt.release_failed:
        notes.append(f"⚠ claims: {receipt.release_failed}")
    return notes


def _bulk_lines(receipt: fleet_service.BulkReceipt) -> list[str]:
    """One agent's line in ``--all``'s output, and its notes under it."""
    agent, result = receipt.agent, receipt.result
    if receipt.outcome == "skipped":
        said, mark = f"skipped — {receipt.why}", "·"
    elif receipt.outcome == "failed":
        said, mark = f"failed — {receipt.why}", "✗"
    elif isinstance(result, fleet_service.RestartReceipt):
        if receipt.dead:  # apart from the running ones (#38)
            # The row as the restart ended it: one no listing had recorded yet holds its
            # exit only there (n6).
            code = (
                agent.exit_status if agent.exit_status is not None else result.replaced.exit_status
            )
            before = f"was dead{f' (exit {code})' if code is not None else ''}, "
        else:
            before = "stopped and " if result.was_running else ""
        said, mark = f"{before}restarted — {result.how}", "✓"
    else:
        said, mark = "stopped", "✓"
    lines = [f"  {mark} {agent.label:<24} {agent.role:<10} {said}"]
    if isinstance(result, fleet_service.RestartReceipt):
        # Every note `fleet restart` prints (the account ladder's, the launch replayed), and
        # the ones that say what did not happen marked: each failure is among the notes.
        failures = set(result.failures)
        lines.extend(f"      {'⚠' if note in failures else '·'} {note}" for note in result.notes)
    elif isinstance(result, fleet_service.StopReceipt):
        lines.extend(f"      {note}" for note in _stop_notes(result, receipt.project))
    return lines


def _bulk_json(report: fleet_service.BulkReport) -> dict[str, object]:
    def project_of(project: ProjectInfo) -> dict[str, object]:
        return {"id": project.id, "name": _display_name(project), "codename": project.codename}

    def receipt_of(receipt: fleet_service.BulkReceipt) -> dict[str, object]:
        out: dict[str, object] = {
            "project": project_of(receipt.project),
            "agent": receipt.agent.model_dump(mode="json"),
            "outcome": receipt.outcome,
            "why": receipt.why,
            "was": "dead" if receipt.dead else "running",  # when the plan was read (#38)
        }
        result = receipt.result
        if isinstance(result, fleet_service.RestartReceipt):
            out |= _restart_json(result)
        elif isinstance(result, fleet_service.StopReceipt):
            out |= _stop_json(result)  # its `agent` is the row as the stop ended it
        return out

    return {
        "action": report.action,
        "managers_only": report.managers_only,
        "receipts": [receipt_of(receipt) for receipt in report.receipts],
        "unread": [
            {"project": project_of(project), "reason": why} for project, why in report.unread
        ],
        "summary": {outcome: len(report.of(outcome)) for outcome in ("done", "skipped", "failed")},
        "ok": report.ok,
        "interrupted": report.interrupted,
    }


def _restart_json(receipt: fleet_service.RestartReceipt) -> dict[str, object]:
    """One restart as ``fleet restart --json`` prints it, and each of ``restart --all``'s:
    built twice, the two had drifted, and a script written against one broke on the other
    (review of #266, round 3)."""
    return {
        "replaced": receipt.replaced.model_dump(mode="json"),
        "started": receipt.started.model_dump(mode="json"),
        "resumed": receipt.resumed,
        "prompt_typed": fleet_service.prompt_typed_json(receipt.prompt_typed),
        "prompt_delivery": receipt.prompt_delivery,
        "prompt_acked": receipt.prompt_acked,
        "how": receipt.how,
        "failures": receipt.failures,
        "was_running": receipt.was_running,
        "tmux_session": receipt.tmux_session,
        "notes": receipt.notes,
    }


def _first_prompt_json(receipt: fleet_service.SpawnReceipt) -> dict[str, object]:
    """How a spawn's first message went, as ``fleet spawn --json`` prints it: ``prompt_typed``
    as restart and switch say it, ``prompt_delivery`` (``argv`` | ``typed`` | null) and
    ``prompt_acked`` (a turn started on it within ``[fleet] prompt_ack_seconds``)."""
    return {
        "prompt_typed": fleet_service.prompt_typed_json(receipt.prompt_typed),
        "prompt_delivery": receipt.prompt_delivery,
        "prompt_acked": receipt.prompt_acked,
    }


def _first_prompt_line(way: str | None) -> str:
    """The receipt's line for a first message a turn started on."""
    how = "on its command line" if way == "argv" else "typed into its pane"
    return f"first message taken ({how})"


def _stop_json(receipt: fleet_service.StopReceipt) -> dict[str, object]:
    """One stop as ``fleet stop --json`` prints it, and each of ``stop --all``'s."""
    return {
        "agent": receipt.agent.model_dump(mode="json"),
        "claims_released": [task.id for task in receipt.released],
        "release_failed": receipt.release_failed,
        "killed": receipt.killed,
        # An ended agent with no window left was only recorded as stopped (#38): the JSON
        # says so as the human line does (final delta review, n4).
        "marked_only": receipt.marked_only,
    }


def _say_released(console: Console, count: int) -> None:
    """The one line for "claims went back to the board", for stop, shutdown and reap alike."""
    if count:
        console.print(f"  {_released_line(count)}")


def _released_line(count: int) -> str:
    return f"🔓 {count} claimed task(s) released back to the board"


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
                    "held": plan.held,
                    "lost": plan.lost,
                    "lost_why": plan.lost_why,
                    "tmux_unusable": plan.tmux_unusable,
                }
            )
        )
        return
    console = stdout_console()
    if not plan.agents and not plan.sessions:
        console.print("nothing to shut down: no live agents and no fleet tmux sessions")
        return
    if plan.tmux_unusable is not None:
        console.print(
            f"tmux is not usable here ({plan.tmux_unusable}), so nothing can be stopped — "
            "with --forget each row whose socket does not answer is ended on your word:"
        )
    else:
        live = f", leave {len(plan.held)} live" if plan.held else ""
        console.print(
            f"about to shut down {len(plan.agents) - len(plan.held)} agent(s){live} "
            f"and kill {len(plan.sessions)} fleet session(s):"
        )
    names = {p.id: (p.codename or p.root.name or p.id) for p in plan.projects}
    for agent in plan.agents:
        where = names.get(agent.project_id, agent.project_id)
        if agent.id in plan.held:
            console.print(f"  ⚠ {agent.label} · {where} — left live: {plan.held[agent.id]}")
        elif plan.tmux_unusable is not None:
            console.print(f"  ✗ {agent.label} · {where} — recorded lost, unchecked")
        elif agent.id in plan.lost:
            # The run's own reason: "no server answered on socket …", or, under --forget, the
            # word that ends a row another server answers for (review of #259, round 3).
            why = plan.lost_why.get(agent.id) or f"no server answers on '{plan.lost[agent.id]}'"
            console.print(f"  ✗ {agent.label} · {where} — recorded lost: {why}")
        else:
            console.print(f"  💤 {agent.label} · {where} — stopped ({agent.pane_id})")
    for session in plan.sessions:
        console.print(f"  ⌧ tmux session {session}")


def _emit_shutdown(report: fleet_service.ShutdownReport, target: ProjectInfo | None = None) -> None:
    """The result, with the service's reason per row and no claim it did not make.

    ``target`` is the project the shutdown was scoped to, which the commands it gives name
    (review of #259, round 21); ``None`` for ``--all``, every project, which the fleet UI
    lists."""
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
                    "release_failures": report.release_failures,
                    "interrupted": report.interrupted,
                    "paused_cleared": report.paused_cleared,
                    "paused_kept": report.paused_kept,
                    "incomplete_projects": report.incomplete_projects,
                    "late_scan_failed": report.late_scan_failed,
                    "pause_scan_failed": report.pause_scan_failed,
                    # The projects themselves, codename and id: `pause_scan_failed` names them
                    # by display name, which `--project` does not take, so a script could not
                    # tell which to `fleet resume` (review of #259, round 23).
                    "pause_unsettled": [_project_json(p) for p in report.pause_unsettled],
                }
            )
        )
        return
    console = stdout_console()
    partial = _not_down(report)
    # Stuck claims are not "partly shut down" — every row is down — but they
    # are the operator's to act on, and a script gating on the exit code must
    # not read a fleet whose work stays claimed by dead sessions as clean
    # (review of the fold). Said in the header, and a non-zero exit below.
    stuck = (
        f", {len(report.release_failures)} claim release(s) refused"
        if report.release_failures
        else ""
    )
    console.print(
        f"{'⚠' if partial or stuck else '✓'} fleet {'PARTLY ' if partial else ''}shut down: "
        f"{len(report.stopped)} stopped, {len(report.recorded)} recorded lost, "
        f"{len(report.failed)} left live{stuck}; "
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
        # NOT "tmux refused to kill it": round 9 widened this field past a refused
        # `kill-session` to a refused WINDOW kill (a late row's dead pane) and to a
        # query that failed with no kill attempted at all. Each entry carries its
        # own reason where it has one; the line above it must only claim the thing
        # they share — the session is still standing (review of #121, round 9
        # verification).
        console.print(f"  ⚠ session {session} was not taken down")
    for session in report.sessions_left_up:
        console.print(f"  ⚠ session {session} left up: it holds a row left live")
    for session in report.sessions_absent:
        console.print(f"  · session {session} was already gone with its last window")
    _say_released(console, len(report.claims_released))
    for failure in report.release_failures:
        console.print(f"  ⚠ claims of {failure}")
    listed = (
        f"`{project_service.fleet_command('ls', target, '--all')}`"
        if target is not None
        else "the fleet UI (`aisquare`)"
    )
    if report.interrupted:
        console.print(
            f"  ⚠ {report.interrupted} — {listed} shows what is still running; "
            "re-run this to finish"
        )
    for name in report.paused_cleared:
        console.print(f"  ▶ the fleet-paused signal on {name} was cleared")
    for name in report.paused_kept:
        console.print(f"  ⏸ {name} stays fleet-paused: it was not confirmed down")
    if report.late_scan_failed:
        console.print(
            f"  ⚠ the final scan for rows spawned during the shutdown did not run "
            f"({report.late_scan_failed}) — nothing below is confirmed down; re-run once the "
            "store answers"
        )
    if report.pause_scan_failed:
        # "every pause is kept" is true only when the visible-projects lookup
        # itself failed and none was reached. The field also carries PER-PROJECT
        # failures, and those sit beside projects that DID clear — printed right
        # above, which the old line then contradicted. Naming what is still set by
        # what was not cleared holds in both shapes, including a signal nobody
        # could read, which the report deliberately claims nothing about (review
        # of #121, round 9 verification).
        # Each project whose signal was not settled, by its own `resume`: under `--all` the
        # line had none, so a signal left set had no way out but the whole shutdown again
        # (review of #259, round 22). A scoped run names its project, as it always did.
        unsettled = [target] if target is not None else report.pause_unsettled
        resumes = [f"`{project_service.fleet_command('resume', p)}`" for p in unsettled]
        resume = (
            f", or clear {'it' if len(resumes) == 1 else 'them'} with {'; '.join(resumes)}"
            if resumes
            else ""
        )
        console.print(
            f"  ⚠ the fleet-paused signals could not be fully reconciled "
            f"({report.pause_scan_failed}) — any signal not named cleared above is still "
            f"set; re-run once the store answers{resume}"
        )
    if partial:
        console.print(
            f"  rows above marked LEFT LIVE were NOT ended: {listed}, then "
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
        bool,
        typer.Option(
            "--all", help="Every project's fleet, not just this one (not with --project)."
        ),
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Shut down without asking; required off a terminal.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Kill every agent without a graceful /exit.")
    ] = False,
    forget: Annotated[
        bool,
        typer.Option(
            "--forget",
            help="End on your word the rows of agents this cannot reach — a server whose "
            "socket is gone, or no usable tmux here — instead of leaving them live or "
            "refusing. Only for agents you know are not running.",
        ),
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

    This project by default, `--all` for every project — one or the other,
    never both. It prints what it would end and asks first at a terminal; off a
    terminal it is a dry run unless --yes, and under --json without --yes it
    prints the plan and changes nothing. Board notes and tasks are kept, the
    ended rows' claims are released, and a `fleet-paused` signal is cleared.
    Each agent's server is asked at the socket its row recorded. A row whose
    agent may still run though its server did not answer (its socket gone) is
    left live with what to do, and with no usable tmux it refuses; `--forget`
    ends those rows on your word, for agents you know are not running. Exits
    1 when any row was left live or a claim release was refused, and 130
    when interrupted — with the report of how far it got either way.
    """
    refuse_conflicting_scope(every, project)
    target = None if every else _project(project)
    if not yes:
        try:
            plan = fleet_service.shutdown_plan(target, forget=forget)
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
        # The prompt names BOTH numbers: a plan with no live agent and one
        # leftover session asked "Shut down 0 agents?" while the real effect —
        # killing the session — sat only in the lines above (round 8 of #203).
        # Nor does it count a row the run leaves live (round 28 of #257).
        ending = len(plan.agents) - len(plan.held)
        parts = []
        if ending:
            parts.append(f"{ending} agent{'s' if ending != 1 else ''}")
        if plan.sessions:
            parts.append(
                f"{len(plan.sessions)} tmux session{'s' if len(plan.sessions) != 1 else ''}"
            )
        if not parts:
            # Every row is left live and no session goes: the run would change nothing,
            # and would say the fleet is not down, as this does.
            stdout_console().print("nothing to shut down: every agent above is left live")
            raise typer.Exit(code=1)
        live = f", leaving {len(plan.held)} live" if plan.held else ""
        if not typer.confirm(f"Shut down {' and '.join(parts)}{live}?", default=False):
            stdout_console().print("nothing stopped")
            return
    try:
        report = fleet_service.shutdown(
            target, force=force, forget=forget, actor=fleet_service.acting_as()
        )
    except fleet_service.FleetInterrupted as exc:
        # The operator's Ctrl-C: rows before it are down and their claims
        # released, committed as they went, so they are told how far it got —
        # and the code says it was not the whole fleet (round 5).
        _emit_shutdown(exc.report, target)
        raise typer.Exit(code=130) from None
    except fleet_service.FleetError as exc:
        _fail_fleet(exc)
    _emit_shutdown(report, target)
    if _not_down(report) or report.release_failures:
        # The fleet is not down — or it is, with claims stuck on ended sessions.
        # Said in the report AND in the exit code, so a script that only reads
        # the code cannot mistake either for a clean run.
        raise typer.Exit(code=1)


def _not_down(report: fleet_service.ShutdownReport) -> bool:
    """Whether the report leaves anything of the fleet standing or unverified."""
    return bool(
        report.failed
        or report.sessions_failed
        or report.late_scan_failed
        or report.pause_scan_failed
        or report.interrupted
    )


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
            f"`{project_service.fleet_command('attach', target, '--json')}` to see the command",
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
        bool,
        typer.Option(
            "--all", help="Every project's fleet, not just this one (not with --project)."
        ),
    ] = False,
    server_down: Annotated[
        bool,
        typer.Option(
            "--server-down",
            help="The private tmux server is gone (reboot, kill-server): mark every live "
            "row on a socket that does not answer as lost, but for one whose agent may "
            "still run (its socket file taken from under its server), which is left live "
            "and named. Without it a silent server marks nothing, because it may be alive "
            "under another TMUX_TMPDIR. A row whose socket another tmux server answers on "
            "is left live and named with or without this flag.",
        ),
    ] = False,
) -> None:
    """Record exited agents, mark vanished panes lost, remove finished worktrees.

    A finished worktree is an ended agent's tree that is clean and whose commits
    are on the default branch or pushed to any remote branch. Its `fleet/…` branch
    stays until the default branch has its work, and is named until then; a branch
    no agent's row recorded is never touched. A tree with uncommitted changes or
    unpushed commits is kept, and named with why. This project by default, `--all`
    for every project — never both. Exits 1 when a released claim could not be given
    back to the board.
    """
    refuse_conflicting_scope(every, project)
    target = None if every else _project(project)
    try:
        report = fleet_service.reap(target, server_down=server_down)
    except fleet_service.FleetError as exc:
        _fail_fleet(exc)
    if get_state().json_output:
        typer.echo(
            json.dumps(
                {
                    "ended": [a.model_dump(mode="json") for a in report.ended],
                    "lost": [a.model_dump(mode="json") for a in report.lost],
                    "worktrees_removed": [str(p) for p in report.worktrees_removed],
                    "worktrees_kept": [
                        {"path": str(path), "reason": reason}
                        for path, reason in report.worktrees_kept
                    ],
                    "branches_deleted": [
                        {"repo": str(root), "branch": branch}
                        for root, branch in report.branches_deleted
                    ],
                    "branches_kept": [
                        {"repo": str(root), "branch": branch}
                        for root, branch in report.branches_kept
                    ],
                    "claims_released": list(report.claims_released),
                    "release_failures": list(report.release_failures),
                    "held": [_row_json(row) for row in report.held],
                }
            )
        )
        if report.release_failures:
            raise typer.Exit(code=1)
        return
    console = stdout_console()
    held = f", {len(report.held)} left live" if report.held else ""
    console.print(
        f"✓ reaped: {len(report.ended)} ended, {len(report.lost)} lost, "
        f"{len(report.worktrees_removed)} worktrees removed{held}"
    )
    # Counts alone do not tell a human WHICH agent went — name them.
    for agent in report.ended:
        code = f" (exit {agent.exit_status})" if agent.exit_status is not None else ""
        console.print(f"  💤 {agent.label}{code}")
    for agent in report.lost:
        console.print(f"  ✗ {agent.label}  pane {agent.pane_id} gone")
    for row in report.held:
        # Its agent may still run where no tmux client reaches it (the one rule `shutdown`
        # and `uninstall` ask): another server answers on its socket, on any reap, or, with
        # --server-down, its socket file is gone or could not be asked. The reason says
        # which, and carries the way out.
        console.print(f"  ⚠ {row.agent.label}  LEFT LIVE — {row.reason}")
    for path in report.worktrees_removed:
        console.print(f"  🗑 {path}")
    for root, branch in report.branches_deleted:
        console.print(f"  🗑 branch {branch} in {root}")
    for root, branch in report.branches_kept:
        # Named on every reap until it goes: reap deletes it once the default branch has
        # its work, and only the operator knows when work it lacks is abandoned. With its
        # repo, as `--all` walks several (review of #259, round 16).
        drop = install_route.command_line(["git", "-C", str(root), "branch", "-D", branch])
        console.print(
            f"  · kept branch {branch} in {root} — commits the default branch lacks; "
            f"a spawn for its task carries on there, and `{drop}` drops it"
        )
    for path, reason in report.worktrees_kept:
        console.print(f"  ⚠ kept {path} — {reason}")
    _say_released(console, len(report.claims_released))
    for failure in report.release_failures:
        console.print(f"  ⚠ claims of {failure}")
    if report.release_failures:
        raise typer.Exit(code=1)  # the same contract as `stop` and `shutdown` (round 5)


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
        f"({fleet_service.session_name(updated.codename or '')})",
        markup=False,
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
    stdout_console().print(f"⏸ fleet paused for {_display_name(target)}", markup=False)


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
    stdout_console().print(f"▶ fleet resumed for {_display_name(target)}", markup=False)


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
