"""``aisquare explainability`` — inspect and join the session-tracing wiring.

``aisquare launch`` wires sessions automatically when the config enables
tracing; these commands cover everything else: ``status`` answers "would a
session launched right now be traced, and if not, why" without launching one,
and ``env`` emits the same env delta as shell exports so a terminal (or a
script) can join a session the launcher does not manage.
"""

from __future__ import annotations

import os
from typing import Annotated

import typer

from aisquare.cli.common import fail
from aisquare.core.config import load_config
from aisquare.services.explainability import probe_proxy, wire_session

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
    """
    wiring = wire_session(
        load_config().explainability,
        role,
        session_id=session_id,
        base_env=dict(os.environ),
    )
    if not wiring.traced:
        fail(wiring.reason, error="untraced")
    for key, value in wiring.env.items():
        typer.echo(f"export {key}={_ansi_c_quoted(value)}")


def _ansi_c_quoted(value: str) -> str:
    """``$'…'`` so the newline between header pairs survives ``eval``.

    Plain single quotes would carry a literal backslash-n; the proxy then sees
    one glued header, ``X-Pipeline-Id`` never arrives, and the run is silently
    recorded under the proxy's default identity — the exact misattribution
    this command exists to prevent.
    """
    escaped = value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
    return f"$'{escaped}'"
