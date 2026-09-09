"""``aisquare agents`` — detect and connect coding agents."""

from __future__ import annotations

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
from aisquare.core.console import stderr_console
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
) -> None:
    """Show integration health for one agent, or all of them."""
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
    except ValueError as exc:
        fail(str(exc), error="not_installed", ref=name)
    emit_connected(connection)


@app.command("disconnect")
def disconnect(name: AgentName, config_dir: ConfigDir = None) -> None:
    """Disconnect an agent (its already-imported context is kept)."""
    try:
        removed = agents_service.disconnect(name, config_dir)
    except KeyError:
        fail(f"unknown agent: {name}", error="unknown_agent", ref=name)
    if not removed:
        stderr_console().print(
            "note: no aisquare hooks found in that config dir — if you connected "
            "with --config-dir, disconnect with the same one"
        )
    emit_disconnected(name)


@app.command("use")
def use(
    name: AgentName,
    project: Annotated[bool, typer.Option("--project", help="Set this project's default.")] = False,
) -> None:
    """Choose the coding agent for future launches (role bindings take precedence)."""
    import json

    from aisquare.core.state import get_state
    from aisquare.services import agent_launch

    try:
        with expected_config_write_errors():
            scope = agent_launch.use(name, project=project)
    except ValueError as exc:
        fail(str(exc), error="unknown_agent")
    if get_state().json_output:
        typer.echo(json.dumps({"agent": name, "scope": scope}))
    else:
        typer.echo(f"Default coding agent: {name} ({scope})")
