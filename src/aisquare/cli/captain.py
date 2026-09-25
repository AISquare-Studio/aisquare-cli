"""``aisquare captain`` — the home-level agent that runs every project's fleet for the owner.

``serve --stdio`` is the Actions MCP server the captain session mounts
(``services.captain.actions``): the only hands the captain has. Spawning or
attaching the captain, the queue verbs and the audit log land on this group
with their own cards.
"""

from __future__ import annotations

from typing import Annotated

import typer

from aisquare.cli import captain_verbs
from aisquare.cli.common import fail
from aisquare.cli.serve import dependency_error

app = typer.Typer(
    help="The captain: the home-level agent that runs every project's fleet for you.",
    no_args_is_help=True,
)
captain_verbs.register(
    app
)  # attention, next, resolve, snooze, since, log, uav, wololo, bt, actions (T5)


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
