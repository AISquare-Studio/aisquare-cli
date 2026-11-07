"""``aisquare agents`` — detect and connect coding agents."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from aisquare.cli.common import (
    emit_agents,
    emit_connected,
    emit_disconnected,
    expected_config_write_errors,
    fail,
)
from aisquare.core import agents as agent_core
from aisquare.core.agents import AgentSettingsError
from aisquare.core.console import stderr_console, stdout_console
from aisquare.core.state import get_state
from aisquare.services import agents as agents_service

app = typer.Typer(help="Detect and connect coding agents.", no_args_is_help=True)

AgentName = Annotated[str, typer.Argument(help="Agent name, e.g. 'claude-code'.")]


@app.command("list")
def list_() -> None:
    """List supported agents and whether they are connected."""
    emit_agents(agents_service.list_agents())


@app.command("scan")
def scan() -> None:
    """Scan this machine for installed agents."""
    emit_agents(agents_service.scan())


@app.command("status")
def status(
    name: Annotated[str | None, typer.Argument(help="Agent to inspect (default: all).")] = None,
    roots: Annotated[
        bool,
        typer.Option(
            "--roots",
            help="List the folders an agent that sandboxes its tools could write, where a "
            "fleet worktree runs none of a repository's hooks.",
        ),
    ] = False,
) -> None:
    """Show integration health for one agent, or all of them."""
    if roots:
        _list_roots(name)
        return
    try:
        agents = agents_service.status(name)
    except KeyError:
        fail(f"unknown agent: {name}", error="unknown_agent", ref=name)
    emit_agents(agents)


ConfigDir = Annotated[
    Path | None,
    typer.Option(
        "--config-dir",
        help="Agent config directory. Default: its native environment override or home.",
    ),
]


@app.command("connect")
def connect(name: AgentName, config_dir: ConfigDir = None) -> None:
    """Connect an agent: install aisquare's hooks and ingest its existing context."""
    try:
        connection = agents_service.connect(name, config_dir)
    except KeyError:
        fail(f"unknown agent: {name}", error="unknown_agent", ref=name)
    except agents_service.UnsupportedAgentError as exc:
        fail(str(exc), error="unsupported_agent", ref=name)
    except agents_service.AgentFileUnreadableError as exc:
        # `detail` too: under --json `fail` drops the message, and asq's Connect
        # button shows the error with its detail, so the file is named there as well.
        fail(str(exc), error="agent_file_unreadable", ref=name, detail=str(exc))
    except AgentSettingsError as exc:
        fail(str(exc), error="agent_configuration", ref=name, detail=str(exc))
    except agents_service.AgentNotInstalledError as exc:
        fail(str(exc), error="not_installed", ref=name)
    emit_connected(connection)
    beside = agents_service.plugin_beside_note(name, config_dir)
    if beside is not None:
        stderr_console().print(f"note: {beside}")


@app.command("disconnect")
def disconnect(
    name: AgentName,
    config_dir: ConfigDir = None,
    forget_root: Annotated[
        Path | None,
        typer.Option(
            "--forget-root",
            help="Forget, and nothing else, that an agent that sandboxes its tools could write "
            "this folder (`agents status --roots`): a fleet worktree in a repository there runs "
            "its hooks again. Asks first; --yes off a terminal.",
        ),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Forget the root without asking.")
    ] = False,
) -> None:
    """Disconnect an agent (its already-imported context is kept)."""
    if forget_root is not None:
        _forget_root(name, forget_root, yes=yes)
        return
    refusal = agents_service.access(name, config_dir).disconnect
    if refusal is not None:
        # Before anything is touched: hooks it cannot take out keep their record too.
        fail(refusal.why, error="agent_file_unreadable", ref=name, detail=refusal.why)
    try:
        removed = agents_service.disconnect(name, config_dir)
    except KeyError:
        fail(f"unknown agent: {name}", error="unknown_agent", ref=name)
    except agents_service.AgentFileUnreadableError as exc:
        fail(str(exc), error="agent_file_unreadable", ref=name, detail=str(exc))
    except AgentSettingsError as exc:
        fail(str(exc), error="agent_configuration", ref=name, detail=str(exc))
    for note in agents_service.disconnect_notes(name, config_dir, removed=removed):
        stderr_console().print(f"note: {note}")
    emit_disconnected(name, trust_left=agent_core.hook_trust_left(name, config_dir))


@app.command("use")
def use(
    name: AgentName,
    project: Annotated[bool, typer.Option("--project", help="Set this project's default.")] = False,
) -> None:
    """Choose the coding agent for future launches (role bindings take precedence)."""
    from aisquare.services import agent_launch

    try:
        with expected_config_write_errors():
            scope = agent_launch.use(name, project=project)
    except ValueError as exc:
        fail(str(exc), error="unknown_agent")
    if get_state().json_output:
        typer.echo(
            json.dumps(
                {
                    "agent": name,
                    "scope": "project" if project else "user",
                    **({"project_id": scope} if project else {}),
                }
            )
        )
    else:
        typer.echo(f"Default coding agent: {name} ({scope})")


@app.command("refresh-hooks", hidden=True)
def refresh_hooks(name: AgentName, config_dir: ConfigDir = None) -> None:
    """Rewrite aisquare's hooks for this version and import nothing.

    Plumbing for ``aisquare upgrade``, which runs it in the NEW install for each
    directory it re-connects. Kept hidden: ``agents connect`` is the command a
    person types. Later releases must keep it, or an upgrade from this one
    cannot refresh hooks.
    """
    from aisquare.core import claude_accounts as accounts_core

    # An upgrade's last step in the new install: the numbers of another agent's accounts a
    # build before the reserve made are kept from 0.9.2 from here on, before any read of the
    # order (#100's reopen 2, A2). Best effort, and said nowhere: this command's output is
    # what every release has printed, which the upgrade that runs it reads.
    accounts_core.reserve_every_number()
    before = _hooks_text(name, config_dir)
    try:
        written = agents_service.refresh_hooks(name, config_dir)
    except KeyError:
        fail(f"unknown agent: {name}", error="unknown_agent", ref=name)
    except agents_service.AgentFileUnreadableError as exc:
        fail(str(exc), error="agent_file_unreadable", ref=name, detail=str(exc))
    except ValueError as exc:
        fail(str(exc), error="not_installed", ref=name)
    if not written:
        fail(f"{name} has no hooks for aisquare to write", error="no_hooks", ref=name)
    # "Rewritten" only for a file that changed: one already current is left as it was, and
    # said so (SMOKE6).
    changed = before is None or before != _hooks_text(name, config_dir)
    if get_state().json_output:
        # As every release has printed it: the upgrade that runs this reads it.
        typer.echo(json.dumps({"name": name, "hooks_installed": True}))
    elif changed:
        stdout_console().print(f"✓ hooks rewritten for {name}")
    else:
        stdout_console().print(f"✓ hooks already current for {name}")


def _hooks_text(name: str, config_dir: Path | None) -> bytes | None:
    """The hooks file ``name``'s hooks live in under ``config_dir``, as bytes; ``None`` where
    there is none or it cannot be read."""
    from aisquare.core import agents as agent_core

    try:
        spec = agent_core.spec(name, config_dir)
        if spec is None or spec.settings_path is None:
            return None
        return spec.settings_path.read_bytes()
    except (OSError, KeyError, ValueError):
        return None


def _known_or_refused(name: str) -> None:
    """Refuse an agent this CLI does not know, as ``agents status`` and ``disconnect`` refuse
    one without ``--roots`` or ``--forget-root`` (#101's reopen 1)."""
    if agent_core.detect(name) is None:
        fail(f"unknown agent: {name}", error="unknown_agent", ref=name)


def _agent_said(name: str) -> str:
    """``a Codex agent``, as the spawn's line names one; an agent this release has no adapter
    for, by its name."""
    from aisquare.core.agent_adapters import recorded_adapter

    known = recorded_adapter(name)
    return f"a {known.label} agent" if known is not None else f"a {name} agent"


def _list_roots(name: str | None) -> None:
    """``agents status --roots``: every recorded folder (``core.sandbox_roots``), each with the
    command that forgets it; of ``name``'s agent alone when named (:func:`sandbox_roots.owner`,
    which a root whose agent cannot be read is said under), and an agent with none says so
    of itself alone (#101's reopen 1)."""
    from aisquare.core import sandbox_roots
    from aisquare.core.shell_line import command_line

    if name is not None:
        _known_or_refused(name)
    every = agents_service.sandbox_roots()
    found = [root for root in every if name is None or sandbox_roots.owner(root) == name]
    if get_state().json_output:
        typer.echo(
            json.dumps(
                [
                    {
                        "path": str(root.path),
                        "agent": root.agent or None,
                        "recorded_at": root.at.isoformat() if root.at is not None else None,
                        "forget": command_line(sandbox_roots.forget_argv(root)),
                    }
                    for root in found
                ]
            )
        )
        return
    console = stdout_console()
    if not found and name is not None:
        # By its display name, as `agents list` says it ("for Cursor"), and the others' roots
        # said in a sentence of their own (R9g2).
        spec = agent_core.spec(name)
        label = spec.label if spec is not None else name
        others = len(every)
        if others == 1:
            also = " 1 is recorded for another agent: aisquare agents status --roots"
        elif others:
            also = f" {others} are recorded for other agents: aisquare agents status --roots"
        else:
            also = ""
        console.print(f"no folder is recorded for {label}.{also}", markup=False)
        return
    if not found:
        console.print("no folder is recorded: every fleet worktree runs its repository's hooks")
        return
    console.print(
        "folders an agent that sandboxes its tools could write; a fleet worktree in a repository "
        "inside one, or holding one, runs none of its hooks:",
        markup=False,
    )
    for root in found:
        when = root.at.astimezone().date().isoformat() if root.at is not None else "?"
        console.print(f"  {root.path}  {root.agent or '?'}  {when}", markup=False)
        console.print(
            f"    forget it: {command_line(sandbox_roots.forget_argv(root))}", markup=False
        )


def _forget_root(name: str, path: Path, *, yes: bool) -> None:
    """``agents disconnect NAME --forget-root PATH``: forget that recorded folder, and nothing
    else, asked first. NAME counts: it must be the agent the root is forgotten as
    (:func:`sandbox_roots.owner`), which the line and the list name (#101's reopen 1)."""
    from aisquare.core import sandbox_roots
    from aisquare.core.shell_line import command_line

    _known_or_refused(name)
    real = Path(path).expanduser().resolve()
    known = next((root for root in agents_service.sandbox_roots() if root.path == real), None)
    if known is None:
        fail(
            f"no folder is recorded at {real} — see: aisquare agents status --roots",
            error="no_such_root",
            ref=str(real),
        )
    owner = sandbox_roots.owner(known)
    if owner != name:
        who = _agent_said(known.agent) if known.agent else "an agent whose name cannot be read"
        fail(
            f"{real} was recorded by {who}, not {name}: forget it with "
            f"{command_line(sandbox_roots.forget_argv(known))}",
            error="wrong_agent",
            ref=str(real),
            detail=owner,
        )
    if not yes:
        if get_state().json_output or not sys.stdin.isatty():
            fail(
                f"forgetting {real} needs --yes off a terminal",
                error="confirmation_required",
                ref=str(real),
            )
        who = _agent_said(known.agent) if known.agent else "an agent that sandboxes its tools"
        if not typer.confirm(
            f"Forget that {who} could write {real}? A fleet worktree in a repository there "
            "runs its hooks again.",
            default=False,
        ):
            stdout_console().print("nothing forgotten", markup=False)
            return
    agents_service.forget_sandbox_root(real)
    if get_state().json_output:
        typer.echo(json.dumps({"forgotten": str(real), "agent": known.agent or None}))
        return
    stdout_console().print(f"✓ forgot {real}", markup=False)
