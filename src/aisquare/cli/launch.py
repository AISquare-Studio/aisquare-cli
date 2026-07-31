"""``aisquare launch`` — start an agent session with its orchestrator role set.

The documented way to join a session to the board was
``AISQUARE_ROLE=coder claude``: an env-var-prefixed launch, retyped in every
terminal, that silently produces an ordinary unattached session when you
forget it. ``aisquare launch coder`` is the same thing with the footgun
removed — it validates the role, opts the repo in explicitly, then *replaces*
this process with the agent so signals, job control and the TTY behave exactly
as if you had run the agent yourself.

Anything after the role is forwarded untouched: ``aisquare launch coder
--model opus`` runs ``claude --model opus``.
"""

from __future__ import annotations

import os
import shutil
from typing import Annotated

import typer

from aisquare.cli.common import fail
from aisquare.core.console import stderr_console
from aisquare.services import team as team_service
from aisquare.services.team import TeamDisabledError

ROLES = ("planner", "coder", "runner")
"""Roles with a standing work cycle the orchestrator injects on every prompt."""

DEFAULT_AGENT = "claude"


def _exec(binary: str, argv: list[str], env: dict[str, str]) -> None:
    """Replace this process with the agent (indirection so tests can intercept)."""
    os.execve(binary, argv, env)


def launch(
    ctx: typer.Context,
    role: Annotated[
        str,
        typer.Argument(help=f"Team role for this session: {', '.join(ROLES)}."),
    ],
    command: Annotated[
        str,
        typer.Option("--command", "-c", help="Agent command to launch.", metavar="CMD"),
    ] = DEFAULT_AGENT,
) -> None:
    """Launch an agent session already attached to this project's team board.

    Equivalent to ``AISQUARE_ROLE=<role> <command>``, plus role validation and
    an explicit opt-in for the repo. Extra arguments are passed to the agent.
    """
    if role not in ROLES:
        fail(
            f"unknown role {role!r} — expected one of: {', '.join(ROLES)}",
            error="unknown_role",
        )
    binary = shutil.which(command)
    if binary is None:
        fail(
            f"{command!r} is not on your PATH — install it, or pass --command",
            error="agent_not_found",
        )
    # A role launch is the opt-in for this repo (same contract the hooks use),
    # so make it explicit and visible here rather than a side effect later.
    try:
        project = team_service.activate()
    except TeamDisabledError as exc:
        fail(str(exc), error="team_disabled")

    env = {**os.environ, "AISQUARE_ROLE": role}
    argv = [command, *ctx.args]
    stderr_console().print(
        f"Launching {command} as [bold]{role}[/bold] on the "
        f"{project.root.name or project.id} board…"
    )
    _exec(binary, argv, env)


def register(app: typer.Typer) -> None:
    """Attach ``launch`` to ``app``, forwarding unknown options to the agent."""
    app.command(
        "launch",
        context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    )(launch)
