"""Root Typer application: global flags, version, and command registration."""

from __future__ import annotations

from typing import Annotated

import typer

from aisquare import __version__
from aisquare.cli import (
    agents,
    auth,
    capture,
    connectors,
    context,
    enforce,
    hook,
    policy,
    project,
    root,
    serve,
    task,
    team,
)
from aisquare.cli import config as config_cli
from aisquare.core.state import RuntimeState, set_state

app = typer.Typer(
    help="Portable memory layer for coding agents.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    pretty_exceptions_show_locals=False,
)


def _print_version(value: bool) -> None:
    """Eager callback for ``--version``."""
    if value:
        typer.echo(f"aisquare {__version__}")
        raise typer.Exit()


@app.callback()
def main_callback(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Show the version and exit.",
            callback=_print_version,
            is_eager=True,
        ),
    ] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Enable verbose output.")
    ] = False,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Suppress non-essential output.")
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON on stdout.")
    ] = False,
    profile: Annotated[
        str, typer.Option("--profile", help="Configuration profile to use.", metavar="NAME")
    ] = "default",
    no_color: Annotated[bool, typer.Option("--no-color", help="Disable coloured output.")] = False,
) -> None:
    """Portable memory layer for coding agents.

    Keeps user preferences and per-project conventions persistent across
    agent sessions.
    """
    state = RuntimeState(
        verbose=verbose,
        quiet=quiet,
        json_output=json_output,
        profile=profile,
        no_color=no_color,
    )
    set_state(state)
    ctx.obj = state


root.register(app)

app.add_typer(auth.app, name="auth")
app.add_typer(agents.app, name="agents")
app.add_typer(connectors.app, name="connectors")
app.add_typer(context.app, name="context")
app.add_typer(context.app, name="ctx", hidden=True, help="Alias of 'context'.")
app.add_typer(project.app, name="project")
app.add_typer(project.app, name="workspace", hidden=True, help="Alias of 'project'.")
app.add_typer(capture.app, name="capture")
app.add_typer(config_cli.app, name="config")
app.add_typer(policy.app, name="policy")
app.add_typer(enforce.app, name="enforce")
app.add_typer(team.app, name="team")
app.add_typer(task.app, name="task")
app.command("note")(team.note)
app.command("board")(team.board)
app.command("recall")(team.recall)
app.command("serve")(serve.serve)
app.add_typer(hook.app, name="hook", hidden=True)


def main() -> None:
    """Console-script entry point for both ``aisquare`` and ``asq``."""
    app()
