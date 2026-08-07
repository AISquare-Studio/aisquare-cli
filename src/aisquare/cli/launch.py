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
from pathlib import Path
from typing import Annotated

import typer

from aisquare.cli.common import fail
from aisquare.core.config import load_config
from aisquare.core.console import stderr_console
from aisquare.services import explainability as explainability_service
from aisquare.services import team as team_service
from aisquare.services.team import TeamDisabledError

ROLES = ("planner", "coder", "runner")
"""Roles with a standing work cycle the orchestrator injects on every prompt."""

DEFAULT_AGENT = "claude"


def _exec(binary: str, argv: list[str], env: dict[str, str]) -> None:
    """Replace this process with the agent (indirection so tests can intercept)."""
    os.execve(binary, argv, env)


def _forwarded_session_id(args: list[str]) -> str | None:
    """The agent's own ``--session-id``, when the caller passed one.

    Reusing it as the trace's pipeline id gives the board row and the
    dashboard Run the same key. We only ever read the forwarded args — never
    inject flags into them, because ``--command`` may name an agent that does
    not speak claude's CLI.
    """
    for position, arg in enumerate(args):
        if arg == "--session-id" and position + 1 < len(args):
            return args[position + 1]
        if arg.startswith("--session-id="):
            return arg.split("=", 1)[1]
    return None


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
    account: Annotated[
        Path | None,
        typer.Option(
            "--account",
            "-a",
            help="Claude config directory to run under, e.g. ~/.claude-account1 "
            "(sets CLAUDE_CONFIG_DIR). Use this for parallel accounts.",
            metavar="DIR",
        ),
    ] = None,
) -> None:
    """Launch an agent session already attached to this project's team board.

    Equivalent to ``AISQUARE_ROLE=<role> <command>``, plus role validation and
    an explicit opt-in for the repo. Extra arguments are passed to the agent.

    ``--account`` selects one of several parallel agent installs. People
    usually reach these through shell aliases (``alias claude1='…'``), which
    ``--command`` cannot resolve — aliases are not executables — so point at
    the config directory instead.
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
    whose = ""
    if account is not None:
        resolved = account.expanduser()
        if not resolved.is_dir():
            # A typo here would not fail loudly — the agent would just start a
            # fresh, unauthenticated profile in a directory it creates.
            fail(
                f"no such config directory: {resolved}",
                error="unknown_account",
            )
        env["CLAUDE_CONFIG_DIR"] = str(resolved)
        whose = f" ({resolved.name})"
    tracing = load_config().explainability
    if tracing.enabled:
        # Fail-open by contract: wire_session returns an empty env delta (plus
        # the reason) rather than raising, so a dead or wrong proxy can only
        # ever cost the trace, never the launch. Disabled config skips even
        # this block — the default launch stays byte-identical.
        wiring = explainability_service.wire_session(
            tracing,
            role,
            session_id=_forwarded_session_id(ctx.args),
            base_env=env,
        )
        env.update(wiring.env)
        stderr_console().print(f"[dim]explainability: {wiring.reason}[/dim]")
    argv = [command, *ctx.args]
    stderr_console().print(
        f"Launching {command}{whose} as [bold]{role}[/bold] on the "
        f"{project.root.name or project.id} board…"
    )
    _exec(binary, argv, env)


def register(app: typer.Typer) -> None:
    """Attach ``launch`` to ``app``, forwarding unknown options to the agent."""
    app.command(
        "launch",
        context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    )(launch)
