"""``aisquare explainability`` — inspect and join the session-tracing wiring.

``aisquare launch`` wires sessions automatically when the config enables
tracing; these commands cover everything else: ``status`` answers "would a
session launched right now be traced, and if not, why" without launching one,
and ``env`` emits the same env delta as shell exports so a terminal (or a
script) can join a session the launcher does not manage.

``env``'s output is quoted for POSIX ``sh``, not for bash. It is composed into
printed spawn commands that people paste anywhere and that CI runs through
``/bin/sh`` — and on Debian and Ubuntu that is dash, where bash's ``$'…'``
form is not special at all: the value arrives with a literal ``$`` in front
and a literal backslash-n where the header separator should be. The proxy then
reads one glued header, never sees ``X-Pipeline-Id``, and files the run under
its default identity. POSIX single quotes carry a real newline in every shell,
which is the only reason ``shlex.quote`` is used here rather than something
prettier.
"""

from __future__ import annotations

import os
import shlex
from typing import Annotated

import typer

from aisquare.cli.common import fail
from aisquare.core.config import load_config
from aisquare.services.explainability import SESSION_ID_ENV_VAR, probe_proxy, wire_session

app = typer.Typer(
    help="Session tracing through the explainability proxy.",
    no_args_is_help=True,
)


@app.command()
def status() -> None:
    """Show the tracing config and whether the proxy would accept a session.

    Exits non-zero only when tracing is enabled but the proxy probe fails —
    the state where launches would silently fall back to untraced.
    """
    settings = load_config().explainability
    verdict = probe_proxy(settings.proxy_url)
    typer.echo(f"enabled:  {settings.enabled}")
    typer.echo(f"proxy:    {settings.proxy_url}")
    typer.echo(f"identity: {settings.agent_name_template}")
    typer.echo(f"probe:    {'healthy' if verdict.healthy else verdict.reason}")
    if settings.enabled and not verdict.healthy:
        raise typer.Exit(code=1)


@app.command()
def env(
    role: Annotated[
        str,
        typer.Argument(help="Role identity for the traced session, e.g. 'coder'."),
    ],
    session_id: Annotated[
        str | None,
        typer.Option("--session-id", help="Key the Run to this session id."),
    ] = None,
) -> None:
    """Print shell exports that trace the next agent run from this terminal.

    Use as ``eval "$(aisquare explainability env coder)"``. Unlike the
    launcher, this refuses loudly (exit 1) when the session would not be
    traced — a human asked for tracing explicitly, so silence would lie.

    ``AISQUARE_SESSION_ID`` is exported alongside the header pair so the
    command that follows can start the agent ON that id
    (``claude --session-id "$AISQUARE_SESSION_ID"``) and have its board row
    join the Run. Exported rather than printed as a comment because the
    output's only job is to be eval'd.
    """
    wiring = wire_session(
        load_config().explainability,
        role,
        session_id=session_id,
        base_env=dict(os.environ),
    )
    if not wiring.traced:
        fail(wiring.reason, error="untraced")
    exports = dict(wiring.env)
    if wiring.pipeline_id:
        exports[SESSION_ID_ENV_VAR] = wiring.pipeline_id
    for key, value in exports.items():
        typer.echo(f"export {key}={shlex.quote(value)}")
