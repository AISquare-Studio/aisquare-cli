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
from aisquare.core import outbox
from aisquare.core.config import ExplainabilityTarget, load_config, save_config
from aisquare.core.state import get_state
from aisquare.services import explainability_ops as ops
from aisquare.services import explainability_proxy as proxy_service
from aisquare.services.explainability import (
    RESERVED_ENV_VARS,
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
    # WHERE the counters count, resolved once for both renderings. The counter
    # says "spool" and the directory is `queue/`: the word is this codebase's
    # (insight_sweeper, "drain the spool") and the path is not it, which cost a
    # senior engineer ninety minutes and produced a false "the spool is empty"
    # while the record was on disk. Nothing shipped points at a WRONG path —
    # the gap was that the tool never said the right one.
    #
    # Fail open: this is decoration on a status line, and `status`'s exit code
    # has exactly one documented meaning (tracing on, proxy refusing). A home
    # that cannot be resolved costs the path, never the command.
    try:
        queue_dir: str | None = str(outbox.queue_dir())
    except Exception:
        queue_dir = None
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
                    # `key_set` alone said "the named variable holds a key",
                    # which stopped being true when the resolver gained the
                    # key-file fallback. `key_source` is the field a script
                    # should branch on; `key_env` stays the variable the target
                    # NAMES, set or not.
                    "key_source": target.key_source,
                    "key_origin": target.key_origin,
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
                        # Beside the counters rather than at the top level: a
                        # script that reads the numbers is the one that wants
                        # the directory, and a second home for the same subject
                        # would give this payload two answers.
                        "queue_dir": queue_dir,
                    },
                },
                separators=(",", ":"),
            )
        )
    else:
        typer.echo(f"enabled:  {settings.enabled}")
        typer.echo(f"target:   {target.name}")
        typer.echo(f"gateway:  {target.gateway_url or '(unset)'} [{target.gateway_source}]")
        typer.echo(f"key:      {target.key_origin} {'is set' if target.api_key else 'is NOT set'}")
        typer.echo(f"proxy:    {target.proxy_url}")
        typer.echo(f"identity: {target.agent_name_template}")
        typer.echo(f"agents:   {', '.join(target.agent_names) or '(none)'}")
        typer.echo(f"probe:    {proxy.summary}")
        typer.echo(f"shipping: {state.reason}")
        # On THIS line and not a new one: "how much is queued" and "where is it"
        # are one question, and the empty case is exactly when someone goes
        # looking — so the path is printed at 0 queued too.
        located = f" — {queue_dir}" if queue_dir else ""
        typer.echo(
            f"spool:    {state.queued} queued, {state.sent} sent, {state.dead} dead-letter{located}"
        )
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
    if get_state().json_output:
        # Deliberately the field NAMES `status` already publishes rather than a
        # second vocabulary for the same facts: two runbooks tell the operator
        # to read `.shipping.gateway` off `status`, and a script that learns
        # `gateway` there should not have to learn `gateway_url` here.
        typer.echo(
            json.dumps(
                {
                    "enabled": settings.enabled,
                    "target": resolved.name,
                    "gateway": resolved.gateway_url,
                    "key_env": resolved.api_key_env,
                    "key_set": bool(resolved.api_key),
                    "key_source": resolved.key_source,
                    "key_origin": resolved.key_origin,
                    "proxy": resolved.proxy_url,
                    "identity": resolved.agent_name_template,
                    "agents": list(resolved.agent_names),
                }
            )
        )
        return
    typer.echo(f"✓ tracing enabled for target '{resolved.name}'")
    typer.echo(f"  gateway:  {resolved.gateway_url or '(unset)'}")
    typer.echo(f"  key from: {resolved.key_origin} {'(set)' if resolved.api_key else '(NOT set)'}")
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
    # Config is ours; the operator's shell is not. §5 tells them to export these,
    # so after `disable` the config says off while THIS shell still routes model
    # traffic through the proxy — and the next rollback step stops that proxy,
    # leaving every launch here pointed at a dead port. The launcher cannot help:
    # an ANTHROPIC_* with no marker beside it is a gateway the operator set up and
    # is theirs to keep, and the tracing block is skipped entirely when config is
    # off so the default launch stays byte-identical. A child cannot unset a
    # variable in its parent's shell either, so telling is the only honest move.
    #
    # Narrow on purpose: only when the value IS the proxy this machine was
    # configured to use, and only when that proxy was CHOSEN. Without the second
    # condition this fires on the shipped default 127.0.0.1:9090 — the address
    # this project documents as someone else's long-running proxy — and tells an
    # operator to unset a variable pointing at their own service.
    #
    # Decided ONCE, above both renderings. It was briefly written twice — the
    # condition in the JSON branch and again below — which is two answers to one
    # question waiting for someone to edit one of them. `status` states the same
    # rule about its spool counters for the same reason.
    target = ops.resolve_target(config.explainability, None)
    ambient = os.environ.get(RESERVED_ENV_VARS[0])
    stale = (
        ambient
        if ambient and ambient == target.proxy_url and target.proxy_source != "default"
        else None
    )

    if get_state().json_output:
        # The stale-export warning is the only thing here an operator ACTS on,
        # so it survives into the machine-readable form rather than being
        # dropped as decoration. An explicit null says "checked, nothing set",
        # which a caller cannot otherwise tell from "never looked".
        typer.echo(
            json.dumps(
                {
                    "enabled": False,
                    "target": target.name,
                    "stale_shell_export": (
                        None
                        if stale is None
                        else {"variable": RESERVED_ENV_VARS[0], "value": stale}
                    ),
                }
            )
        )
        return

    typer.echo("✓ tracing disabled — sessions launch untraced, targets left in place")
    if stale is not None:
        names = " ".join(RESERVED_ENV_VARS)
        typer.echo(
            f"  note: this shell still exports {names.split()[0]}={stale} — "
            "launches from here keep using the proxy and will fail once it stops. "
            f"We cannot change your shell: unset {names}"
        )


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
    if get_state().json_output:
        # Every FAILURE above answers in JSON through the shared `fail` helper,
        # so before this the flag was honoured on five branches that fail and
        # ignored on the one that works — §1 of the runbook, and the first step
        # that touches the gateway.
        #
        # One key, and it is the code's own word for the mapping
        # (`publication_ids`). The two cases the human output distinguishes —
        # an id, or registered with none — are an id or a null, so a caller
        # reads `.publications["aisquare-coder"]` and gets the same answer the
        # operator reads. A separate list of names would repeat the keys.
        typer.echo(
            json.dumps(
                {
                    "target": target.name,
                    "publications": {name: published.get(name) for name in names},
                }
            )
        )
        return
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
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Exit non-zero when this run could not ship at all — for timers.",
        ),
    ] = False,
) -> None:
    """Drain buffered insights to the gateway (prompts, notes, task events).

    This is the only place the CLI talks to the gateway. It is deliberately a
    separate command and a separate process: the capture seams buffer, this
    delivers, and a gateway that is down therefore costs a delay rather than a
    prompt. Exits non-zero only when records were dead-lettered — a deferral is
    the design working, not a failure.
    """
    report = ship_once(limit=limit)
    if get_state().json_output:
        # The whole report. `blocked` is in it because its own comment says it
        # is set rather than inferred "so no caller has to match on message
        # text" — and a timer wrapper reading stdout is exactly that caller.
        typer.echo(
            json.dumps(
                {
                    "sent": report.sent,
                    "deferred": report.deferred,
                    "dead": report.dead,
                    "runs": list(report.runs),
                    "reason": report.reason,
                    "blocked": report.blocked,
                }
            )
        )
    else:
        typer.echo(report.reason)
        if report.runs:
            typer.echo(f"runs: {', '.join(report.runs)}")
    if report.dead:
        raise typer.Exit(code=1)
    # Opt-in, because the quiet default is doctrine: no key or config means
    # nothing captured and nothing logged as an error, and a non-zero default
    # would mail every operator who deliberately does not ship. A TIMER wants
    # the opposite — cron discards stdout by convention, so exit 0 is the only
    # thing it reads, and a blocked run reporting healthy forever is how the
    # insight lane goes silently missing while the proxy lane looks perfect.
    # Fires on BLOCKED only: a deferral is retried by the next tick, and
    # shouting about a transient outage teaches the operator to ignore the mail.
    if strict and report.blocked:
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
    if get_state().json_output:
        # The refusal above already answered in JSON, through the shared `fail`
        # helper — so before this, the flag was honoured on the branch that
        # FAILS and ignored on the branch that WORKS. A script piping this into
        # jq passed every test while the proxy was down and broke on the day it
        # came up.
        #
        # The variables are the payload, under one key, spelled exactly as they
        # are exported: a caller reads `.env.AISQUARE_PIPELINE_ID`, which is the
        # name §5 of the runbook already uses. Lifting the pipeline id to a
        # second top-level field would give the same value two names.
        typer.echo(json.dumps({"role": role, "env": exports}))
        return
    for key, value in exports.items():
        typer.echo(f"export {key}={shlex.quote(value)}")


proxy_app = typer.Typer(
    help="Run the local claude_code proxy the launcher points sessions at.",
    no_args_is_help=True,
)
app.add_typer(proxy_app, name="proxy")


@proxy_app.command("up")
def proxy_up() -> None:
    """Start the proxy from this machine's configured gateway, key and port.

    Everything it needs is already in config — this exists so that starting a
    sidecar is one command rather than four exported variables assembled by
    hand, which is what the runbook asked for and what people got wrong.

    Exits non-zero when it could not start one. That is the opposite of the
    launch path's fail-open rule and deliberately so: nothing here runs in front
    of a waiting developer, and a `up` that quietly started nothing would be
    discovered as untraced sessions hours later.
    """
    try:
        state = proxy_service.up()
    except proxy_service.ProxyError as exc:
        fail(str(exc), error="proxy_not_started")
    if get_state().json_output:
        typer.echo(json.dumps(_proxy_payload(state)))
        return
    # `up` only returns on a managed state now — it rolls back and raises
    # otherwise — so this line is no longer an unconditional claim. It used to be
    # printed beside a summary reading "not running", because `up` returned
    # `status()` whatever it said.
    typer.echo(f"✓ {state.summary}")
    typer.echo(
        "  Sessions launched from now on are traced. Stop it with: "
        "aisquare explainability proxy down"
    )


@proxy_app.command("down")
def proxy_down() -> None:
    """Stop the proxy this CLI started.

    Leaves a proxy it did not start alone, and says so. Stopping a process
    because something is listening on the port is how you end a colleague's
    session on a shared box, or kill a hosted proxy this machine was pointed at
    deliberately.
    """
    try:
        outcome = proxy_service.down()
    except proxy_service.ProxyError as exc:
        fail(str(exc), error="proxy_not_stopped")
    if get_state().json_output:
        typer.echo(json.dumps({"result": outcome}))
        return
    typer.echo(outcome)


@proxy_app.command("status")
def proxy_status() -> None:
    """Say whether a proxy is up, and whether it is one we started.

    The distinction is the point. ``doctor``'s proxy row goes green when *a*
    service answers as ``aisquare-proxy`` in ``claude_code`` mode — it cannot
    tell this machine's sidecar from one left running last week against another
    deployment, and Runs recorded by the wrong proxy are attributed to the wrong
    place. This command answers that question specifically.

    Exit code follows the useful predicate: 0 when a healthy proxy is answering,
    1 when nothing is, so a timer or a shell `&&` can depend on it.
    """
    state = proxy_service.status()
    if get_state().json_output:
        typer.echo(json.dumps(_proxy_payload(state)))
    else:
        # Three states, not two. The mark used to follow `probe.healthy`, which
        # put a ✓ on a proxy serving the PREVIOUS deployment — the exact case
        # this command exists to surface, rendered as though nothing was wrong.
        if state.managed:
            mark = "✓"
        elif state.probe.healthy:
            mark = "⚠"
        else:
            mark = "✗"
        typer.echo(f"{mark} {state.summary}")
        if state.probe.healthy and not state.managed:
            typer.echo(f"  → this machine's config points at {state.url} (target '{state.target}')")
    if not state.probe.healthy:
        raise typer.Exit(code=1)


def _proxy_payload(state: proxy_service.ProxyStatus) -> dict[str, object]:
    """One shape for both proxy commands, so a script reads them the same way."""
    return {
        "healthy": state.probe.healthy,
        # `managed` is the field worth branching on: healthy alone cannot tell
        # you the proxy is the one this machine configured.
        "managed": state.managed,
        "url": state.url,
        "target": state.target,
        "gateway": state.gateway_url,
        "pid": state.pid,
        "age_seconds": round(state.age_seconds, 1) if state.age_seconds is not None else None,
        "summary": state.summary,
    }
