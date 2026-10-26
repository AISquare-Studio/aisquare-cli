"""Root Typer application: global flags, version, and command registration.

Bare ``aisquare`` / ``asq`` at a terminal opens the fleet UI; in a pipe or with
``TERM=dumb`` it prints usage and exits 2 exactly as it always did, so scripts
never meet a full-screen app (docs/plans/fleet-tui.md §3.8). Under ``--json``
the same refusal is one JSON object, because there stdout belongs to a program.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer

from aisquare.cli import (
    accounts,
    agents,
    auth,
    brief,
    captain,
    capture,
    code,
    command_reports,
    connectors,
    context,
    enforce,
    explainability,
    fleet,
    hook,
    launch,
    persona,
    policy,
    project,
    remote,
    root,
    serve,
    task,
    team,
    watcher,
)
from aisquare.cli import config as config_cli
from aisquare.cli import metrics as metrics_cli
from aisquare.cli.global_flags import DISPATCHED_WORDS, GlobalFlagsGroup
from aisquare.core import feature_use, sharing
from aisquare.core.state import RuntimeState, get_state, set_state
from aisquare.core.version import __version__

app = typer.Typer(
    cls=GlobalFlagsGroup,
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
    """One terminal over your projects and your coding agents: task a manager in
    plain words, and it runs the coders, testers and reviewers for you.

    Underneath, a memory keeps your preferences and each project's conventions
    across sessions.
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
    else:
        _count_command(ctx)


#: Commands that tools run for the user, on every prompt or for the life of a session: what
#: they do is not a choice of the user's, so they are not counted as features used. And
#: ``uninstall``, whose plan must change no file of the home and whose purge removes it.
_NOT_FEATURES = frozenset({"hook", "serve", "inject", "uninstall"})


#: The detached shipper itself: not a feature, and it must not start another.
_SHIPPER = "cli.metrics.ship"


def command_path(ctx: typer.Context) -> str | None:
    """The command being run as ``cli.<group>.<verb>``, built from the NAMES of the commands
    it resolves to and nothing the user typed: a word counts only when it names a subcommand
    of the group before it, and the walk stops at the first one that does not (an argument,
    or an option's value). ``None`` for a command that is not a feature (:data:`_NOT_FEATURES`).
    """
    name = ctx.invoked_subcommand
    if name is None or name in _NOT_FEATURES:
        return None
    names = [name]
    # Duck-typed: Typer 0.27's groups are not click's ``Group``.
    command: Any = (
        ctx.command.get_command(ctx, name) if hasattr(ctx.command, "get_command") else None
    )
    rest = list(ctx.meta.get(DISPATCHED_WORDS, []))[1:]  # the first word is `name`
    while hasattr(command, "get_command"):
        words = [word for word in rest if not word.startswith("-")]
        child = command.get_command(ctx, words[0]) if words else None
        if child is None:
            break
        names.append(child.name or words[0])
        rest = rest[rest.index(words[0]) + 1 :]
        command = child
    return "cli." + ".".join(names)


def _count_command(ctx: typer.Context) -> None:
    """Count this command in the local feature counters: an append to a spool file, failing
    open (``core.feature_use``)."""
    try:
        path = command_path(ctx)
        if path is not None and path != _SHIPPER:
            feature_use.record(path)
            sharing.start_background_ship()  # a detached `metrics ship`, once in 6 h if on
    except Exception:  # a counter never costs a command
        return


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
app.add_typer(accounts.app, name="accounts")
app.add_typer(connectors.app, name="connectors", hidden=True)
app.add_typer(context.app, name="context")
app.add_typer(context.app, name="ctx", hidden=True, help="Alias of 'context'.")
app.add_typer(project.app, name="project")
app.add_typer(project.app, name="workspace", hidden=True, help="Alias of 'project'.")
app.add_typer(capture.app, name="capture", hidden=True)
app.add_typer(config_cli.app, name="config")
app.add_typer(metrics_cli.app, name="metrics", hidden=True)
app.add_typer(policy.app, name="policy", hidden=True)
app.add_typer(enforce.app, name="enforce", hidden=True)
app.add_typer(team.app, name="team")
app.add_typer(task.app, name="task")
app.add_typer(brief.app, name="brief")
app.command("note")(team.note)
app.command("board")(team.board)
app.command("recall")(team.recall)
launch.register(app)  # needs context_settings to forward agent args
command_reports.register(app)  # `exec` forwards everything after -- to the command
app.command("serve")(serve.serve)
app.add_typer(remote.app, name="remote")
app.add_typer(fleet.app, name="fleet")
app.add_typer(watcher.app, name="watch")
app.add_typer(code.app, name="code")
app.add_typer(persona.app, name="persona")
app.add_typer(captain.app, name="captain")
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
