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
import re
import shutil
from typing import Annotated

import typer

from aisquare.cli.common import fail
from aisquare.core import harness
from aisquare.core.config import load_config
from aisquare.core.console import stderr_console
from aisquare.services import explainability as explainability_service
from aisquare.services import team as team_service
from aisquare.services.team import TeamDisabledError

ROLES = ("planner", "coder", "runner")
"""Roles with a standing work cycle the orchestrator injects on every prompt."""

#: A numbered SEAT of a first-class role — ``coder1``, ``coder2``. Crews run
#: several agents in the same role and need to tell them apart on the board;
#: the work cycle is the role's, so the number is an identity, not a new role.
_SEAT = re.compile(rf"^({'|'.join(ROLES)}|validator)\d+$")

DEFAULT_AGENT = "claude"


def _declared_roles() -> set[str]:
    """Roles the operator has named in ``team.profiles``.

    Declaring a role in config IS the operator saying it exists, so honouring
    it here keeps one source of truth instead of two lists that drift.
    """
    try:
        return set(load_config().team.profiles)
    except Exception:  # fail-open: a broken config must not block a launch
        return set()


def _role_ok(role: str) -> bool:
    """First-class role, a numbered seat of one, or declared in config.

    The whitelist earns its keep by catching typos — ``codr`` silently
    producing an unattached session was the original footgun — so this stays a
    check rather than becoming free-form. It just stops rejecting the two
    shapes real crews use: numbered seats, and roles the operator has already
    written down.
    """
    return role in ROLES or bool(_SEAT.match(role)) or role in _declared_roles()


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
        str | None,
        typer.Option(
            "--command",
            "-c",
            help="Agent command to launch. Overrides the role's bound `bin`; "
            f"defaults to that, then to `{DEFAULT_AGENT}`.",
            metavar="CMD",
        ),
    ] = None,
    env_pairs: Annotated[
        list[str] | None,
        typer.Option(
            "--env",
            "-e",
            help="KEY=VALUE to set for this launch (repeatable). Merges per key over "
            "the role's bound profile.",
            metavar="KEY=VALUE",
        ),
    ] = None,
) -> None:
    """Launch an agent session already attached to this project's team board.

    Equivalent to ``AISQUARE_ROLE=<role> <command>``, plus role validation and
    an explicit opt-in for the repo. Extra arguments are passed to the agent.

    The role's bound profile (``aisquare team bind``) supplies its binary, env
    and extra args. ``--command`` overrides the binary and ``--env KEY=VALUE``
    adds to or overrides the environment, both for one launch only.

    Parallel agent installs are usually reached through shell aliases, which
    ``--command`` cannot resolve — an alias is not an executable — so bind the
    variables the alias sets and keep the ordinary binary.
    """
    if not _role_ok(role):
        fail(
            f"unknown role {role!r} — expected one of: {', '.join(ROLES)}, "
            "a numbered seat of one (coder1, coder2), or a role you have "
            "bound with `aisquare team bind`",
            error="unknown_role",
        )
    # Resolve WHICH executable on the same ladder `team spawn` uses, so a role
    # bound to a wrapper launches on it here too. This used to read `--command`
    # alone and ignore the binding entirely, which is worse than not supporting
    # it: the docstring promised the profile supplied the binary, so `launch`
    # silently started the DEFAULT agent under the right role name and exited 0.
    resolution = harness.resolve_binary(role, override=command)
    binary = shutil.which(resolution.binary)
    if binary is None:
        # Name the candidate AND who chose it — a bare "not on your PATH" sends
        # the reader hunting through flag, env and config to learn which won.
        fail(
            f"{resolution.binary!r} is not on your PATH (chosen by: {resolution.source}) "
            "— install it, pass --command, or change the role's binding",
            error="agent_not_found",
        )
    # A role launch is the opt-in for this repo (same contract the hooks use),
    # so make it explicit and visible here rather than a side effect later.
    try:
        project = team_service.activate()
    except TeamDisabledError as exc:
        fail(str(exc), error="team_disabled")

    env = {**os.environ, "AISQUARE_ROLE": role}
    # The role's bound spec plus this launch's overrides, carried verbatim.
    # Resolved even with no flag, so a bound role launches correctly without
    # the operator remembering to say anything.
    try:
        overrides = harness.parse_env_pairs(env_pairs or [])
    except ValueError as exc:
        fail(str(exc), error="bad_env_pair")
    profile = harness.resolve_profile(role, env_overrides=overrides)
    if profile.notice is not None:
        # No silent fail-soft: unreadable config means this role launches
        # UNBOUND — possibly on a different install than the operator believes.
        stderr_console().print(
            f"[dim]role bindings: config unreadable ({profile.notice}) — launching unbound[/dim]"
        )
    env.update(profile.env)
    whose = f" ({','.join(sorted(profile.env))})" if profile.env else ""
    try:
        tracing = load_config().explainability
    except Exception as exc:  # tracing is an observer: a broken config must
        # cost the trace, never the launch — the same fail-open bar as a dead
        # proxy. `aisquare doctor` still reports the config error loudly.
        tracing = None
        stderr_console().print(
            f"[dim]explainability: config unreadable ({exc}) — launching untraced[/dim]"
        )
    if tracing is not None and tracing.enabled:
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
    argv = [resolution.binary, *profile.args, *ctx.args]
    stderr_console().print(
        f"Launching {resolution.binary}{whose} as [bold]{role}[/bold] on the "
        f"{project.root.name or project.id} board…"
    )
    _exec(binary, argv, env)


def register(app: typer.Typer) -> None:
    """Attach ``launch`` to ``app``, forwarding unknown options to the agent."""
    app.command(
        "launch",
        context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    )(launch)
