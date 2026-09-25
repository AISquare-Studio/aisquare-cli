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
import sys
from typing import Annotated, Any

import typer
from typer.core import TyperGroup

from aisquare.cli import captain_verbs, captain_voice
from aisquare.cli.common import fail
from aisquare.cli.serve import dependency_error
from aisquare.core.console import stderr_console, stdout_console
from aisquare.core.state import get_state

NO_TEXT = "the captain's turn ended without text — its pane shows what it did"
"""What ``say`` reports for a turn that answered with tools alone: said, never as the reply."""


class _SayByDefault(TyperGroup):
    """A first word that names no subcommand is a message: ``captain "what is up"`` means
    ``captain say "what is up"``. Only the group's own help stays the group's.

    Rewritten BEFORE the group parses (``parse_args``): at ``resolve_command`` the
    group had already refused ``--timeout`` as its own unknown option and eaten a
    ``--`` that was to let a message start with ``-``. So ``captain --timeout 30
    "what is up"`` and ``captain -- "-5 degrees"`` both reach ``say`` whole.

    ``ctx`` is typed ``Any``: click is vendored (``typer._click``), and like
    ``cli/global_flags.py`` this stays on the public typer surface."""

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        if args and args[0] == "--voice":
            # The plan's spelling of the voice page (T3, rider 13143 (1)): the leaf,
            # not a message to the captain that starts with "--voice".
            args = ["voice", *args[1:]]
        if args and args[0] not in self.commands and args[0] not in ctx.help_option_names:
            args = ["say", *args]
        result: list[str] = super().parse_args(ctx, args)
        return result


app = typer.Typer(
    cls=_SayByDefault,
    help="The captain: the home-level agent that runs every project's fleet for you. "
    'Bare, it starts or attaches to the captain; `aisquare captain "text"` asks it.',
    invoke_without_command=True,
    no_args_is_help=False,
)
captain_voice.register(app)  # `voice`: the page, in its own module (T3)
captain_verbs.register(
    app
)  # attention, next, resolve, snooze, since, log, uav, wololo, bt, actions (T5)


@app.callback()
def captain(ctx: typer.Context) -> None:
    """Start the home's captain, or attach to it when it is already running."""
    if ctx.invoked_subcommand is not None:
        return
    from aisquare.cli.fleet import _exec_attach, _fail_fleet, interactive_terminal
    from aisquare.services import fleet as fleet_service
    from aisquare.services.captain import brain
    from aisquare.services.captain import state as captain_state

    as_json = get_state().json_output
    console = stdout_console()
    try:
        agent = brain.find()
        receipt = brain.start() if agent is None else None
        attaching = as_json or interactive_terminal()
        argv = fleet_service.attach_argv(captain_state.home_project()) if attaching else []
    except fleet_service.FleetError as exc:
        _fail_fleet(exc)
    if as_json:
        # One object, never an exec — `fleet attach --json`'s rule: the caller runs argv.
        live = receipt.agent if receipt is not None else agent
        assert live is not None  # found, or just started
        typer.echo(
            json.dumps(
                {
                    "agent": live.model_dump(mode="json"),
                    "started": receipt is not None,
                    "tmux_session": receipt.tmux_session if receipt is not None else None,
                    "notes": receipt.notes if receipt is not None else [],
                    "argv": argv,
                }
            )
        )
        return
    if receipt is not None:
        console.print(
            f"✓ started the captain ({receipt.agent.id}) in tmux session {receipt.tmux_session}",
            markup=False,
            highlight=False,
        )
        for note in receipt.notes:
            console.print(f"  {note}", markup=False, highlight=False)
    elif agent is not None:
        console.print(f"the captain is already running ({agent.id})", markup=False, highlight=False)
    if not attaching:
        console.print(
            "attach from a terminal with `aisquare captain`; ask it with "
            '`aisquare captain "what is up"`',
            markup=False,
            highlight=False,
        )
        return
    sys.stdout.flush()
    try:
        _exec_attach(argv)  # the fleet's one attach seam (core.spawn.SEAMS: EXCLUDED)
    except OSError as exc:  # tmux vanished between the service's check and the exec
        fail(
            f"could not run {argv[0]}: {exc} — is tmux installed and on PATH?",
            error="fleet_unavailable",
            detail=str(exc),
        )


@app.command("say")
def say(
    words: Annotated[list[str], typer.Argument(help="What to say to the captain.")],
    timeout: Annotated[
        float, typer.Option("--timeout", min=1, help="Seconds to wait for the reply.")
    ] = 180.0,
) -> None:
    """Say something to the captain and print its reply.

    Under ``--json``: one object, ``{"reply", "ended_at", "timed_out"}`` — the reply,
    when the answering turn ended, and whether it timed out. With no reply, ``"said"``
    says why and the exit is 1; ``timed_out`` is false when waiting longer would not
    have helped (the captain died, tmux refused the keys). A turn that ended without
    text is an answer (exit 0) whose ``reply`` is null, and ``"said"`` says so.
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
            typer.echo(json.dumps({"reply": None, "ended_at": None, "timed_out": exc.timed_out,
                                   "said": str(exc)}))  # fmt: skip
            raise typer.Exit(1) from exc
        fail(str(exc), error="captain_no_reply")
    except fleet_service.FleetError as exc:
        fail(str(exc), error="captain_unavailable", detail=str(exc))
    if as_json:
        ended = reply.ended_at.isoformat() if reply.ended_at is not None else None
        body: dict[str, object] = {"reply": reply.text, "ended_at": ended, "timed_out": False}
        if reply.text is None:
            body["said"] = NO_TEXT
        typer.echo(json.dumps(body))
        return
    if reply.text is None:
        stderr_console().print(NO_TEXT, markup=False, highlight=False, style="dim")
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
        if reply.text is None:
            console.print(f"({NO_TEXT})", markup=False, highlight=False, style="dim")
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
