"""``aisquare explainability`` — inspect, join and wire the session tracing.

``aisquare launch`` wires sessions automatically when the config enables
tracing; these commands cover everything else. ``status`` answers "would a
session launched right now be traced, and if not, why" without launching one,
``env`` emits the same env delta as shell exports so a terminal (or a script)
can join a session the launcher does not manage, ``enable`` is the one command
that turns tracing on for this machine, and ``register`` declares this
machine's agent identities to a workspace so its spans are routable at all.

Keys are read from the environment variable the active target names, used for
the one call that needs them, and never written down or echoed.
"""

from __future__ import annotations

import os
from typing import Annotated

import typer

from aisquare.cli.common import fail
from aisquare.core.config import ExplainabilityTarget, load_config, save_config
from aisquare.services import explainability_ops as ops
from aisquare.services.explainability import probe_proxy, wire_session

app = typer.Typer(
    help="Session tracing through the explainability proxy.",
    no_args_is_help=True,
)

_TARGET_OPTION = typer.Option("--target", help="Deployment to act on, e.g. stg or prod.")


@app.command()
def status(
    target_name: Annotated[str | None, _TARGET_OPTION] = None,
) -> None:
    """Show the tracing config and whether the proxy would accept a session.

    Exits non-zero only when tracing is enabled but the proxy probe fails —
    the state where launches would silently fall back to untraced.
    """
    settings = load_config().explainability
    target = ops.resolve_target(settings, target_name)
    verdict = probe_proxy(target.proxy_url)
    typer.echo(f"enabled:  {settings.enabled}")
    typer.echo(f"target:   {target.name}")
    typer.echo(f"gateway:  {target.gateway_url or '(unset)'} [{target.gateway_source}]")
    typer.echo(f"key:      ${target.api_key_env} {'is set' if target.api_key else 'is NOT set'}")
    typer.echo(f"proxy:    {target.proxy_url}")
    typer.echo(f"identity: {target.agent_name_template}")
    typer.echo(f"agents:   {', '.join(target.agent_names) or '(none)'}")
    typer.echo(f"probe:    {'healthy' if verdict.healthy else verdict.reason}")
    if settings.enabled and not verdict.healthy:
        raise typer.Exit(code=1)


@app.command()
def enable(
    target_name: Annotated[str | None, _TARGET_OPTION] = None,
    gateway_url: Annotated[
        str | None,
        typer.Option("--gateway-url", help="Explainability gateway base URL for the target."),
    ] = None,
    key_env: Annotated[
        str | None,
        typer.Option(
            "--key-env",
            help="Name of the environment variable holding the workspace key. "
            "The key itself is never stored.",
        ),
    ] = None,
    proxy_url: Annotated[
        str | None,
        typer.Option(
            "--proxy-url", help="claude_code proxy the launcher should point sessions at."
        ),
    ] = None,
    identity: Annotated[
        str | None,
        typer.Option("--identity", help="Agent name template, e.g. 'aisquare-{role}'."),
    ] = None,
) -> None:
    """Turn session tracing on for this machine (and set up a target).

    This is the one command that flips the switch. Every option is optional:
    with none of them it just enables tracing against the configured target,
    and repeating it with ``--target prod --gateway-url …`` is how a machine
    gains a second deployment without editing config by hand.
    """
    config = load_config()
    settings = config.explainability
    name = target_name or settings.target
    if target_name:
        settings.target = target_name

    if gateway_url or key_env or proxy_url or identity:
        target = settings.targets.get(name, ExplainabilityTarget())
        if gateway_url:
            target.gateway_url = gateway_url.rstrip("/")
        if key_env:
            target.api_key_env = key_env
        if proxy_url:
            target.proxy_url = proxy_url
        if identity:
            target.agent_name_template = identity
        settings.targets[name] = target

    settings.enabled = True
    save_config(config)

    resolved = ops.resolve_target(settings, name)
    typer.echo(f"✓ tracing enabled for target '{resolved.name}'")
    typer.echo(f"  gateway:  {resolved.gateway_url or '(unset)'}")
    typer.echo(
        f"  key from: ${resolved.api_key_env} {'(set)' if resolved.api_key else '(NOT set)'}"
    )
    typer.echo(f"  proxy:    {resolved.proxy_url}")
    typer.echo(f"  agents:   {', '.join(resolved.agent_names) or '(none)'}")
    typer.echo("  next:     aisquare doctor --live")


@app.command()
def disable() -> None:
    """Turn session tracing off for this machine (targets are kept)."""
    config = load_config()
    config.explainability.enabled = False
    save_config(config)
    typer.echo("✓ tracing disabled — sessions launch untraced, targets left in place")


@app.command()
def register(
    target_name: Annotated[str | None, _TARGET_OPTION] = None,
    role: Annotated[
        list[str] | None,
        typer.Option("--role", help="Role to register; repeat for several. Defaults to config."),
    ] = None,
) -> None:
    """Declare this machine's agent identities to the workspace.

    Spans whose ``agent.name`` the workspace does not know are rejected, so a
    fresh deployment traces nothing until this runs. Idempotent: an already
    registered name returns its existing publication id.
    """
    settings = load_config().explainability
    target = ops.resolve_target(settings, target_name)
    if not target.gateway_url:
        fail(
            f"target '{target.name}' has no gateway URL — set one with: "
            f"aisquare explainability enable --target {target.name} --gateway-url <url>",
            error="unconfigured",
        )
    if not target.api_key:
        fail(
            f"${target.api_key_env} is not set in this shell — export the workspace key "
            "there (it is never stored by the CLI) and re-run",
            error="no-key",
        )

    if role:
        try:
            names = tuple(target.agent_name_template.format(role=r) for r in role)
        except (KeyError, IndexError, ValueError) as exc:
            fail(
                f"agent_name_template {target.agent_name_template!r} is invalid ({exc})",
                error="bad-template",
            )
    else:
        names = target.agent_names
    if not names:
        fail("no agent identities to register — check explainability.roles", error="no-agents")

    verdict = ops.register_roster(target, names)
    if not verdict.ok:
        fail(
            f"registration refused by {target.gateway_url}: {verdict.detail}",
            error="register-failed",
            hint="a workspace key is required here; a studio-scoped key cannot declare a roster",
        )

    published = ops.publication_ids(verdict.payload)
    typer.echo(f"✓ registered {len(names)} identities with target '{target.name}'")
    for agent_name in names:
        publication = published.get(agent_name)
        suffix = f"publication_id {publication}" if publication else "registered"
        typer.echo(f"  {agent_name}: {suffix}")
    if not published:
        typer.echo("  (the workspace returned no publication ids; re-run after it syncs)")


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
