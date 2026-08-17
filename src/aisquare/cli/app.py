"""Root Typer application: global flags, version, and command registration."""

from __future__ import annotations

from typing import Annotated

import typer

from aisquare.cli import (
    agents,
    auth,
    capture,
    connectors,
    context,
    enforce,
    explainability,
    hook,
    launch,
    policy,
    project,
    root,
    serve,
    task,
    team,
)
from aisquare.cli import config as config_cli
from aisquare.cli.global_flags import GlobalFlagsGroup
from aisquare.core.state import RuntimeState, set_state
from aisquare.core.version import __version__

app = typer.Typer(
    cls=GlobalFlagsGroup,
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
        bool,
        typer.Option(
            "--json",
            help="Emit machine-readable JSON on stdout. Put --json before the "
            "subcommand to also get usage errors (unknown command/option) as JSON.",
        ),
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

# Roadmap groups (auth, connectors, capture, policy, enforce) are hidden: every
# leaf still reports the not-implemented contract when invoked, but listing them
# in --help alongside working commands made a third of the surface look real.
app.add_typer(auth.app, name="auth", hidden=True)
app.add_typer(agents.app, name="agents")
app.add_typer(connectors.app, name="connectors", hidden=True)
app.add_typer(context.app, name="context")
app.add_typer(context.app, name="ctx", hidden=True, help="Alias of 'context'.")
app.add_typer(project.app, name="project")
app.add_typer(project.app, name="workspace", hidden=True, help="Alias of 'project'.")
app.add_typer(capture.app, name="capture", hidden=True)
app.add_typer(config_cli.app, name="config")
app.add_typer(policy.app, name="policy", hidden=True)
app.add_typer(enforce.app, name="enforce", hidden=True)
app.add_typer(team.app, name="team")
app.add_typer(task.app, name="task")
app.command("note")(team.note)
app.command("board")(team.board)
app.command("recall")(team.recall)
launch.register(app)  # needs context_settings to forward agent args
app.command("serve")(serve.serve)
app.add_typer(hook.app, name="hook", hidden=True)
# Hidden while explainability.enabled defaults to off; flips visible with the
# tracing rollout (prod cutover), same pattern as the roadmap groups above.
app.add_typer(explainability.app, name="explainability", hidden=True)


def main() -> None:
    """Console-script entry point for both ``aisquare`` and ``asq``."""
    app()
