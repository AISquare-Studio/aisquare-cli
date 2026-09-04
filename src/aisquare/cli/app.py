"""Root Typer application: global flags, version, and command registration.

Bare ``aisquare`` / ``asq`` at a terminal opens the fleet UI; in a pipe or with
``TERM=dumb`` it prints usage and exits 2 exactly as it always did, so scripts
never meet a full-screen app (docs/plans/fleet-tui.md §3.8). Under ``--json``
the same refusal is one JSON object, because there stdout belongs to a program.
"""

from __future__ import annotations

import json
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
    fleet,
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
from aisquare.core.state import RuntimeState, get_state, set_state
from aisquare.core.version import __version__

app = typer.Typer(
    cls=GlobalFlagsGroup,
    help="Portable memory layer for coding agents.",
    no_args_is_help=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    pretty_exceptions_show_locals=False,
)


def _print_version(value: bool) -> None:
    """Eager callback for ``--version``."""
    if value:
        typer.echo(f"aisquare {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
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
    if ctx.invoked_subcommand is None:
        _no_arguments(ctx)


def _no_arguments(ctx: typer.Context) -> None:
    """Bare ``asq``: the fleet UI at a terminal; usage everywhere else, as before.

    Exit 2 with the help text is what ``no_args_is_help`` produced for every
    earlier release, so a script that ran ``aisquare`` by mistake sees nothing
    new. ``--json`` never opens a UI either — a machine asked for a
    machine-readable answer and there is none — but it does not get the help
    page: under ``--json`` stdout belongs to a program (empty, or ONE parseable
    object — ``tests/test_json_stdout_is_machine_readable.py``), and echoing
    ~40 lines of rich-formatted human text there hands a ``jq`` pipeline a
    parse error. It is also what ``no_args_is_help`` never did: click sent its
    ``Missing command.`` to stderr and left stdout empty. So the refusal is the
    same usage object ``global_flags._handle_usage_error`` emits for an unknown
    command or option — same key, same exit code, one line.
    """
    if get_state().json_output:
        typer.echo(json.dumps({"error": "usage", "message": "Missing command."}))
        raise typer.Exit(2)
    if not fleet.interactive_terminal():
        typer.echo(ctx.get_help())
        raise typer.Exit(2)
    fleet.ui()


root.register(app)

# Roadmap groups (connectors, capture, policy, enforce) are hidden: every
# leaf still reports the not-implemented contract when invoked, but listing them
# in --help alongside working commands made a third of the surface look real.
app.add_typer(auth.app, name="auth")
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
app.add_typer(fleet.app, name="fleet")
app.command("ui")(fleet.ui)
app.add_typer(hook.app, name="hook", hidden=True)
# Visible: unlike the roadmap groups above, every leaf here does something on a
# stock machine — `enable` is the one command that turns tracing on, and the
# rest report or wire it. A surface an operator has to be told exists is not an
# operator surface, and the cutover is done by a human reading --help.
app.add_typer(explainability.app, name="explainability")


def main() -> None:
    """Console-script entry point for both ``aisquare`` and ``asq``."""
    app()
