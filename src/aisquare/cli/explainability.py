"""``aisquare explainability`` — inspect, join and wire the session tracing.

``aisquare launch`` wires sessions automatically when the config enables
tracing; these commands cover everything else. ``status`` answers "would a
session launched right now be traced, and if not, why" without launching one,
and ``env`` emits the same env delta as shell exports so a terminal (or a
script) can join a session the launcher does not manage. ``enable`` is the one
command that turns tracing on for this machine, and ``register`` declares this
machine's agent identities to a workspace so its spans are routable at all.

Keys are read from the environment variable the active target names, used for
the one call that needs them, and never written down or echoed.

``env``'s output is quoted for POSIX ``sh``, not for bash. It is composed into
printed spawn commands that people paste anywhere and that CI runs through
``/bin/sh`` — on Debian and Ubuntu that is dash, where bash's ``$'…'`` form is
not special at all. Measured before the fix: ``ANTHROPIC_BASE_URL`` arrived as
``$http://127.0.0.1:9190`` and the agent died with ``API Error: Invalid URL``,
exit 1, nothing reaching the proxy — tracing costing a LAUNCH, which the
fail-open doctrine forbids outright. ``shlex.quote`` single-quotes every byte,
newline included, in every shell.
"""

from __future__ import annotations

import json
import os
import shlex
from typing import Annotated

import typer

from aisquare.cli.common import expected_config_write_errors, fail
from aisquare.core.config import ExplainabilityTarget, load_config, save_config
from aisquare.core.state import get_state
from aisquare.services import explainability_ops as ops
from aisquare.services.explainability import (
    ship_once,
    shipping_state,
    trace_marker,
    wire_session,
)

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

    Honours ``--json``, because this is the command a cutover gets scripted
    against: without it every check in the runbook is a grep against prose,
    and prose is the part most likely to be reworded.
    """
    config = load_config()
    settings = config.explainability
    target = ops.resolve_target(settings, target_name)
    # One description of the proxy lane for both surfaces. It also decides
    # whether to probe at all: a machine that never configured tracing has
    # nothing to dial, and reporting a refused connection to a default address
    # the operator never chose reads as a broken machine when nothing is wrong.
    proxy = ops.proxy_state(target, on=settings.enabled)
    state = shipping_state(target_name)
    level = config.redaction.level
    if get_state().json_output:
        typer.echo(
            json.dumps(
                {
                    "enabled": settings.enabled,
                    "target": target.name,
                    "gateway": target.gateway_url,
                    "gateway_source": target.gateway_source,
                    "key_env": target.api_key_env,
                    "key_set": bool(target.api_key),
                    "proxy": target.proxy_url,
                    "identity": target.agent_name_template,
                    "agents": list(target.agent_names),
                    "probe": proxy.summary,
                    "redaction": str(level),
                    # The spool counters live HERE, not under a top-level
                    # "spool", even though the human view below prints them on
                    # a line of their own. Both surfaces render ONE
                    # shipping_state object; a second path to the same three
                    # integers would give this payload two answers and no
                    # canonical one. The runbook promised `.spool` and it never
                    # existed — `jq -r` answers a missing key with null and
                    # exits 0, so the command written to catch a silent backlog
                    # was itself silent. Fixed on the page rather than here,
                    # which is only safe while these three stay reachable:
                    # tests/test_spool_counters_agree.py pins that they agree
                    # with the human line, and that no top-level "spool"
                    # quietly reappears.
                    "shipping": {
                        "gateway": state.gateway_url,
                        "reason": state.reason,
                        "queued": state.queued,
                        "sent": state.sent,
                        "dead": state.dead,
                    },
                },
                separators=(",", ":"),
            )
        )
    else:
        typer.echo(f"enabled:  {settings.enabled}")
        typer.echo(f"target:   {target.name}")
        typer.echo(f"gateway:  {target.gateway_url or '(unset)'} [{target.gateway_source}]")
        typer.echo(
            f"key:      ${target.api_key_env} {'is set' if target.api_key else 'is NOT set'}"
        )
        typer.echo(f"proxy:    {target.proxy_url}")
        typer.echo(f"identity: {target.agent_name_template}")
        typer.echo(f"agents:   {', '.join(target.agent_names) or '(none)'}")
        typer.echo(f"probe:    {proxy.summary}")
        typer.echo(f"shipping: {state.reason}")
        typer.echo(f"spool:    {state.queued} queued, {state.sent} sent, {state.dead} dead-letter")
        # Directly under the spool counts on purpose: "how much am I sending"
        # and "what is in it" are one question, and an operator who reads the
        # first without the second is the person this line exists for.
        typer.echo(f"redaction: {ops.redaction_summary(level)}")
    # Unchanged rule, same data: non-zero ONLY when tracing is on and the proxy
    # would not take a session — the state where launches silently go untraced.
    if settings.enabled and not proxy.healthy:
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
    with expected_config_write_errors():
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
    with expected_config_write_errors():
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
def ship(
    limit: Annotated[int, typer.Option("--limit", help="Most records to drain in one pass.")] = 500,
) -> None:
    """Drain buffered insights to the gateway (prompts, notes, task events).

    This is the only place the CLI talks to the gateway. It is deliberately a
    separate command and a separate process: the capture seams buffer, this
    delivers, and a gateway that is down therefore costs a delay rather than a
    prompt. Exits non-zero only when records were dead-lettered — a deferral is
    the design working, not a failure.
    """
    report = ship_once(limit=limit)
    typer.echo(report.reason)
    if report.runs:
        typer.echo(f"runs: {', '.join(report.runs)}")
    if report.dead:
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
    target_name: Annotated[str | None, _TARGET_OPTION] = None,
) -> None:
    """Print shell exports that trace the next agent run from this terminal.

    Use as ``eval "$(aisquare explainability env coder)"``. Unlike the
    launcher, this refuses loudly (exit 1) when the session would not be
    traced — a human asked for tracing explicitly, so silence would lie.

    ``AISQUARE_PIPELINE_ID`` is exported alongside the header pair so the
    command that follows can start the agent ON that id
    (``claude --session-id "$AISQUARE_PIPELINE_ID"``) and have its board row
    join the Run. Exported rather than printed as a comment because the
    output's only job is to be eval'd.
    """
    wiring = wire_session(
        ops.effective_settings(load_config().explainability, target_name),
        role,
        session_id=session_id,
        base_env=dict(os.environ),
    )
    if not wiring.traced:
        fail(wiring.reason, error="untraced")
    exports = dict(wiring.env)
    if wiring.pipeline_id:
        # Marks the ANTHROPIC_* beside it as OURS, so a second paste in the
        # same terminal clears our leftovers instead of silently inheriting
        # this session's pipeline id and merging two sessions into one Run.
        exports.update(trace_marker(wiring))
    for key, value in exports.items():
        typer.echo(f"export {key}={shlex.quote(value)}")
