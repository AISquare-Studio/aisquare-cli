"""``aisquare captain`` — the home-level agent that runs every project's fleet for the owner.

- ``aisquare captain``: start the home's captain when there is none, else attach
  to its window (off a terminal: say where it is).
- ``aisquare captain "text"`` / ``aisquare captain say "text"``: deliver the text
  and print the captain's reply (``services.captain.brain.say``). ``say`` is the
  explicit form, for a message whose first word is a subcommand's name.
- ``aisquare captain chat``: a line-by-line conversation over the same delivery.
- ``aisquare captain serve --stdio``: the Actions MCP server the captain mounts
  (``services.captain.actions``) — the only hands the captain has.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Annotated, Any

import typer
from typer.core import TyperGroup

from aisquare.cli.common import fail
from aisquare.cli.serve import dependency_error
from aisquare.core.console import stdout_console
from aisquare.core.state import get_state


class _SayByDefault(TyperGroup):
    """A first word that names no subcommand is a message: ``captain "what is up"`` means
    ``captain say "what is up"``. An option (``--help``) still reaches the group.

    ``ctx`` is typed ``Any``: click is vendored (``typer._click``), and like
    ``cli/global_flags.py`` this stays on the public typer surface."""

    def resolve_command(self, ctx: Any, args: list[str]) -> Any:
        if args and args[0] not in self.commands and not args[0].startswith("-"):
            args = ["say", *args]
        return super().resolve_command(ctx, args)


app = typer.Typer(
    cls=_SayByDefault,
    help="The captain: the home-level agent that runs every project's fleet for you. "
    'Bare, it starts or attaches to the captain; `aisquare captain "text"` asks it.',
    invoke_without_command=True,
    no_args_is_help=False,
)


@app.callback()
def captain(ctx: typer.Context) -> None:
    """Start the home's captain, or attach to it when it is already running."""
    if ctx.invoked_subcommand is not None:
        return
    from aisquare.cli.fleet import _fail_fleet, interactive_terminal
    from aisquare.services import fleet as fleet_service
    from aisquare.services.captain import brain
    from aisquare.services.captain import state as captain_state

    console = stdout_console()
    try:
        agent = brain.find()
        if agent is None:
            receipt = brain.start()
            console.print(
                f"✓ started the captain ({receipt.agent.id}) in tmux session "
                f"{receipt.tmux_session}",
                markup=False,
                highlight=False,
            )
            for note in receipt.notes:
                console.print(f"  {note}", markup=False, highlight=False)
        else:
            console.print(
                f"the captain is already running ({agent.id})", markup=False, highlight=False
            )
        if not interactive_terminal():
            console.print(
                "attach from a terminal with `aisquare captain`; ask it with "
                '`aisquare captain "what is up"`',
                markup=False,
                highlight=False,
            )
            return
        argv = fleet_service.attach_argv(captain_state.home_project())
    except fleet_service.FleetError as exc:
        _fail_fleet(exc)
    sys.stdout.flush()
    os.execvp(argv[0], argv)


@app.command("say")
def say(
    words: Annotated[list[str], typer.Argument(help="What to say to the captain.")],
    timeout: Annotated[
        float, typer.Option("--timeout", min=1, help="Seconds to wait for the reply.")
    ] = 180.0,
) -> None:
    """Say something to the captain and print its reply.

    Under ``--json``: one object, ``{"reply", "ended_at", "timed_out"}`` — the reply,
    when the answering turn ended, and whether it timed out (then ``"said"`` says why,
    and the exit is 1).
    """
    from aisquare.services import fleet as fleet_service
    from aisquare.services.captain import brain

    text = " ".join(words).strip()
    if not text:
        raise typer.BadParameter("nothing to say", param_hint="WORDS")
    as_json = get_state().json_output
    try:
        reply = brain.say(text, timeout=timeout)
    except brain.NoReply as exc:
        if as_json:
            typer.echo(json.dumps({"reply": None, "ended_at": None, "timed_out": True,
                                   "said": str(exc)}))  # fmt: skip
            raise typer.Exit(1) from exc
        fail(str(exc), error="captain_no_reply")
    except fleet_service.FleetError as exc:
        fail(str(exc), error="captain_unavailable")
    if as_json:
        ended = reply.ended_at.isoformat() if reply.ended_at is not None else None
        typer.echo(json.dumps({"reply": reply.text, "ended_at": ended, "timed_out": False}))
        return
    stdout_console().print(reply.text, markup=False, highlight=False)


@app.command("chat")
def chat(
    timeout: Annotated[
        float, typer.Option("--timeout", min=1, help="Seconds to wait for each reply.")
    ] = 180.0,
) -> None:
    """Talk to the captain line by line; an empty line is skipped, end of input ends it."""
    from aisquare.services import fleet as fleet_service
    from aisquare.services.captain import brain

    console = stdout_console()
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        try:
            reply = brain.say(text, timeout=timeout)
        except (brain.NoReply, fleet_service.FleetError) as exc:
            console.print(f"✗ {exc}", markup=False, highlight=False)
            continue
        console.print(reply.text, markup=False, highlight=False)


@app.command("serve")
def serve(
    stdio: Annotated[
        bool,
        typer.Option("--stdio", help="Serve over stdio — how the captain session mounts it."),
    ] = False,
    close_after: Annotated[
        int,
        typer.Option(
            "--close-after",
            min=0,
            help="Exit after this many seconds without a client message or a running tool "
            "call (0 = run forever).",
        ),
    ] = 300,
) -> None:
    """Run the captain's Actions MCP server: the tools the captain acts through."""
    if not stdio:
        raise typer.BadParameter(
            "the captain's server speaks stdio only — run: aisquare captain serve --stdio",
            param_hint="'--stdio'",
        )
    problem = dependency_error()
    if problem is not None:
        fail(problem, error="serve_not_installed")
    from aisquare.core.orchestrator import team_enabled

    if not team_enabled():
        fail(
            "the agent orchestrator is disabled (AISQUARE_TEAM=0) — every captain action "
            "goes through the board, so the captain cannot serve without it",
            error="team_disabled",
        )
    from aisquare.services.captain import actions

    actions.run_stdio(close_after=close_after)
