"""``aisquare team`` — the agent orchestrator for parallel sessions.

Also home of the top-level ``note`` and ``board`` shortcuts (registered by
``cli/app.py``) so agents can type the two most frequent verbs directly.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Annotated, NoReturn

import typer

from aisquare.cli.common import expected_config_write_errors, fail, local_time
from aisquare.core import harness, orchestrator
from aisquare.core.config import ExplainabilitySettings, RoleLaunchProfile, load_config
from aisquare.core.console import stdout_console
from aisquare.core.state import get_state
from aisquare.core.store import (
    AmbiguousIdError,
    StoreUnopenable,
    damaged_data_message,
    damaged_store_message,
    damaged_store_recovery,
    is_corrupt_error,
    is_locked_error,
)
from aisquare.services import explainability as explainability_service
from aisquare.services import explainability_ops
from aisquare.services import settings as settings_service
from aisquare.services import team as team_service
from aisquare.services.team import DeliveryUnconfirmedError, TeamDisabledError

app = typer.Typer(help="Coordinate parallel agent sessions on this project.", no_args_is_help=True)

#: Appended to a PRINTED spawn command so the session the human pastes starts
#: on the very id its Run is keyed by. Expands to ``--session-id <uuid>`` when
#: the ``explainability env`` eval in front of it minted one, and to NOTHING
#: when that eval refused — which is the whole fail-open premise: an empty
#: ``--session-id ''`` would be a broken launch, no flag at all is a normal
#: one. Deliberately unquoted: the value is a UUID, so word splitting produces
#: exactly the two words intended, and ``sh``/``bash``/``zsh`` agree on it.
_SESSION_ID_SUBSTITUTION = (
    f"${{{explainability_service.PIPELINE_ID_ENV_VAR}:+"
    f"--session-id ${explainability_service.PIPELINE_ID_ENV_VAR}}}"
)

#: Clears the PREVIOUS paste's tracing out of this shell, and only ever the
#: previous paste's.
#:
#: A terminal keeps what ``eval`` exported, so running a printed spawn command
#: twice (the up-arrow flow, every time an agent exits) finds ANTHROPIC_* still
#: set. ``wire_session`` then correctly refuses to clobber what looks like the
#: user's own routing — and the second agent silently inherits the FIRST one's
#: ``X-Pipeline-Id`` and merges into its Run. Observed, not theorised: two
#: pastes, one Run.
#:
#: ``AISQUARE_PIPELINE_ID`` is the discriminator, because nothing but our own
#: wiring sets it. Present ⇒ the ANTHROPIC_* beside it are ours to clear.
#: Absent ⇒ they are the operator's real gateway and stay untouched, so the
#: "not overriding your routing" guard keeps working exactly as before.
_CLEAR_PREVIOUS_TRACE = (
    f'if [ -n "${{{explainability_service.PIPELINE_ID_ENV_VAR}:-}}" ]; then '
    f"unset {explainability_service.PIPELINE_ID_ENV_VAR} "
    f"{explainability_service.TRACE_AGENT_NAME_ENV_VAR} "
    f"{' '.join(explainability_service.RESERVED_ENV_VARS)}; fi"
)

SessionRef = Annotated[
    str | None,
    typer.Option("--as", help="Act as this team session (id prefix, from your board)."),
]

# Every failure a store-backed team command can hit, routed through _fail_team.
# DatabaseError covers OperationalError AND the rarer corruption/constraint
# family — none of them may reach the user as a traceback.
STORE_ERRORS = (
    TeamDisabledError,
    KeyError,
    AmbiguousIdError,
    DeliveryUnconfirmedError,
    sqlite3.DatabaseError,
)


def _fail_team(exc: Exception, ref: str | None = None) -> NoReturn:
    """Translate service errors into the shared CLI error contract."""
    if isinstance(exc, TeamDisabledError):
        fail(str(exc), error="team_disabled")
    if isinstance(exc, AmbiguousIdError):
        fail(f"'{exc.ref}' is ambiguous — use more characters", error="ambiguous_id", ref=exc.ref)
    if isinstance(exc, DeliveryUnconfirmedError):
        # ref = the unconfirmed write's id, so an agent knows exactly which
        # event/task to look for in `aisquare log` before retrying.
        fail(str(exc), error="delivery_unconfirmed", ref=exc.ref)
    if isinstance(exc, sqlite3.Error) and is_locked_error(exc):
        # Transient contention: honest "try again", never a traceback —
        # and never anything a caller could mistake for success.
        fail(
            f"context store busy ({exc}) — retry shortly",
            error="store_locked",
            detail=str(exc),
        )
    if isinstance(exc, StoreUnopenable):
        # Legible since it was written, and a dead end until now: "context
        # store error: file is not a database" named no file and no next step.
        # Same sentence doctor prints, from one function.
        fail(
            damaged_store_message(exc),
            error="store_unopenable",
            hint=damaged_store_recovery(),
            detail=str(exc),
        )
    if isinstance(exc, sqlite3.Error) and is_corrupt_error(exc):
        # Reached when a SELECT finds the damage: the store opened, so the
        # StoreUnopenable branch above never fires. Ordered AFTER is_locked_error
        # so contention keeps its own answer.
        fail(
            damaged_data_message(exc),
            error="store_damaged",
            hint=damaged_store_recovery(),
            detail=str(exc),
        )
    if isinstance(exc, sqlite3.DatabaseError):
        # NOT retryable (no such table, readonly, disk full): a distinct code,
        # with the real cause preserved for --json callers.
        fail(f"context store error: {exc}", error="store_error", detail=str(exc))
    if isinstance(exc, KeyError):
        missing = ref if ref is not None else str(exc)
        fail(f"nothing matches '{missing}'", error="not_found", ref=missing)
    raise exc


def emit_write_warning(delivery: team_service.Delivery | None) -> None:
    """Surface a board-mismatch warning on stderr (human and ``--json`` runs).

    Plain ``echo``, not the rich console: the warning must never wrap or be
    reflowed — agents grep their own stderr for it.
    """
    if delivery is not None and delivery.warning:
        typer.echo(f"⚠ {delivery.warning}", err=True)


def delivery_fields(delivery: team_service.Delivery | None) -> dict[str, object]:
    """The top-level JSON fields a confirmed write adds to its payload."""
    if delivery is None:
        return {}
    fields: dict[str, object] = {"delivered": True}
    if delivery.warning:
        fields["warning"] = delivery.warning
    return fields


def receipt_suffix(delivery: team_service.Delivery | None) -> str:
    """The `` · seq N on <board>`` tail of a human ✓ line (empty for reads)."""
    return f" · {delivery.receipt}" if delivery is not None else ""


@app.command("on")
def on() -> None:
    """Activate the orchestrator for this project."""
    try:
        project = team_service.activate()
    except STORE_ERRORS as exc:
        _fail_team(exc)
    delivery = team_service.last_delivery()
    if get_state().json_output:
        payload: dict[str, object] = {"activated": project.id, "root": str(project.root)}
        payload.update(delivery_fields(delivery))
        typer.echo(json.dumps(payload))
    else:
        stdout_console().print(
            f"✓ agent orchestrator active for {project.root.name or project.id} — "
            f"sessions launched here now share tasks and notes{receipt_suffix(delivery)}"
        )


@app.command("status")
def status() -> None:
    """Show the live team board (sessions, tasks, recent updates)."""
    board()


@app.command("focus")
def focus(
    text: Annotated[str, typer.Argument(help="What this session is working on right now.")],
    as_session: SessionRef = None,
) -> None:
    """Announce this session's current focus to the team."""
    if as_session is None:
        fail("--as <session> is required (your id is on the board)", error="missing_session")
    try:
        session = team_service.set_focus(text, as_session)
    except STORE_ERRORS as exc:
        _fail_team(exc, as_session)
    delivery = team_service.last_delivery()
    if get_state().json_output:
        payload = session.model_dump(mode="json")
        payload.update(delivery_fields(delivery))
        typer.echo(json.dumps(payload))
    else:
        stdout_console().print(
            f"✓ focus of {team_service.short_id(session.id)}: {text}{receipt_suffix(delivery)}",
            markup=False,
        )


@app.command("role")
def role(
    name: Annotated[str, typer.Argument(help="Role for this session (planner/coder/runner/…).")],
    as_session: SessionRef = None,
) -> None:
    """Set a session's role on the board."""
    if as_session is None:
        fail("--as <session> is required (your id is on the board)", error="missing_session")
    try:
        session = team_service.set_role(name, as_session)
    except STORE_ERRORS as exc:
        _fail_team(exc, as_session)
    if get_state().json_output:
        typer.echo(session.model_dump_json())
    else:
        stdout_console().print(f"✓ {team_service.short_id(session.id)} is now {session.role}")


def _parse_since(value: str) -> datetime:
    """``--since`` accepts a relative span (15m, 2h, 3d) or an ISO timestamp."""
    span = re.fullmatch(r"(\d+)([smhd])", value.strip())
    if span:
        unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[span.group(2)]
        return datetime.now(tz=UTC) - timedelta(**{unit: int(span.group(1))})
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise typer.BadParameter(
            f"{value!r} is neither a span (15m, 2h) nor an ISO timestamp"
        ) from None
    # A naive timestamp means the user's local clock.
    return moment if moment.tzinfo is not None else moment.astimezone()


def _bin_env_hint(role: str) -> str:
    """The env var that would pin this role's binary — shown in the failure so
    the fix is readable where the problem is."""
    return f"{harness._bin_env_var(role)}=<command>"


def _parse_env(pairs: list[str]) -> dict[str, str]:
    """``KEY=VALUE`` pairs from the command line, or a usage error."""
    try:
        return harness.parse_env_pairs(pairs)
    except ValueError as exc:
        fail(str(exc), error="bad_env_pair")


@app.command("spawn")
def spawn(
    role_name: Annotated[
        str, typer.Argument(help="Role to launch (planner/coder/runner/validator/…).")
    ],
    agent_bin: Annotated[
        str | None,
        typer.Option(
            "--bin",
            help=(
                "Executable that runs the agent (e.g. claude2). Overrides the per-role "
                "map; see `aisquare team harness` for what each role resolves to."
            ),
        ),
    ] = None,
    env_pairs: Annotated[
        list[str] | None,
        typer.Option(
            "--env",
            "-e",
            help="KEY=VALUE to set for this launch (repeatable). Merges per key over "
            "the role's configured profile, so one variable can be changed without "
            "discarding the rest.",
            metavar="KEY=VALUE",
        ),
    ] = None,
    extra_args: Annotated[
        list[str] | None,
        typer.Option(
            "--arg",
            help="Extra argument appended to the agent command (repeatable).",
            metavar="ARG",
        ),
    ] = None,
    execute: Annotated[
        bool,
        typer.Option(
            "--exec",
            help="Replace this process with the launched session (default: print the command).",
        ),
    ] = False,
    probe: Annotated[
        bool | None,
        typer.Option(
            "--probe/--no-probe",
            help="Verify model availability before picking (paid ~1-token probe, cached 24h). "
            "Default: probe, unless AISQUARE_HARNESS_PROBE=0.",
        ),
    ] = None,
    refresh: Annotated[
        bool,
        typer.Option(
            "--refresh",
            help="Ignore cached availability verdicts (use after an entitlement changes).",
        ),
    ] = False,
    effort: Annotated[
        str | None,
        typer.Option(
            "--effort",
            help="Effort for this role (low/medium/high/xhigh/max/ultracode). Absolute — "
            "skips the role offset. Default: the session base (AISQUARE_EFFORT, else "
            "CLAUDE_EFFORT, else high) shifted by the role's offset.",
        ),
    ] = None,
) -> None:
    """Resolve a role's model down its ladder and launch (or print) the session.

    The harness picks the strongest available model for the role — e.g. the
    planner wants fable and falls back to opus, then sonnet, when the account
    doesn't serve it — and passes the role's effort level. Resolution never
    blocks a launch: on any doubt it degrades down the ladder.
    """
    if not orchestrator.team_enabled():
        _fail_team(TeamDisabledError())
    if effort is not None and harness.normalize_effort(effort) is None:
        fail(
            f"unknown --effort '{effort}' — use one of: "
            f"{', '.join([*harness.EFFORT_SCALE, harness.ULTRACODE])}",
            error="bad_effort",
        )
    if refresh:
        # Forget EVERY cached verdict, not just the rungs this walk touches:
        # --refresh promises "re-check after an entitlement change", and an
        # entitlement change is account-wide, not per-role.
        harness.clear_probe_cache()
    if (probe is None and harness.probing_enabled()) or probe:
        # Probes spawn short agent subprocesses; without this line a spawn can
        # sit silent for seconds and read as hung.
        typer.echo("probing model availability (cached 24h; --no-probe skips)…", err=True)
    resolution = harness.resolve_model(role_name, probe=probe, refresh=refresh, effort=effort)
    try:
        tracing: ExplainabilitySettings | None = load_config().explainability
    except Exception as exc:  # fail-open: a broken config costs the trace, never the spawn
        tracing = None
        typer.echo(f"explainability: config unreadable ({exc}) — sessions untraced", err=True)
    env_assignments = [f"AISQUARE_ROLE={shlex.quote(role_name)}"]
    # WHICH executable, resolved separately from WHICH model — flag > env >
    # config > default (#52). Reported in the banner because a role silently
    # launching on a different install than the operator expects is the same
    # class of surprise as a silent model swap.
    binary = harness.resolve_binary(role_name, override=agent_bin)
    # WHOSE install, resolved on the same ladder as the binary. Kept separate
    # because they answer different questions and most setups only need this
    # one: parallel accounts are reached through aliases that set
    # CLAUDE_CONFIG_DIR, and an alias is not something --bin could resolve.
    launch_profile = harness.resolve_profile(
        role_name, env_overrides=_parse_env(env_pairs or []), extra_args=extra_args or []
    )
    if launch_profile.notice is not None:
        # No silent fail-soft: an unreadable config means this role launches
        # UNBOUND, i.e. possibly on a different install than the operator
        # believes. Proceeding quietly would report success for the wrong thing.
        typer.echo(
            f"role bindings: config unreadable ({launch_profile.notice}) — launching unbound",
            err=True,
        )
    # Put the profile's vars IN the printed command, not just in --exec's env.
    # The banner is meant to be pasted, and a printed command that silently
    # launches with different variables than the one it just reported is worse
    # than printing nothing.
    for key, value in launch_profile.env.items():
        env_assignments.append(f"{key}={shlex.quote(value)}")
    if resolution is None:
        argv = [binary.binary, *launch_profile.args]
        banner = f"{role_name}: untiered role — launching on the session default model"
    else:
        argv = [
            binary.binary,
            "--model",
            resolution.model,
            "--effort",
            resolution.effort,
            *launch_profile.args,
        ]
        skipped = f" (skipped: {', '.join(resolution.skipped)})" if resolution.skipped else ""
        profile = harness.ROLE_PROFILES.get(role_name)
        mission = profile.mission if profile else "pinned by AISQUARE_MODEL override"
        banner = (
            f"{role_name}: {resolution.model} @ {resolution.effort} "
            f"[model {resolution.source} · effort {resolution.effort_source}]{skipped} — {mission}"
        )
    if binary.source != "default":
        banner = f"{banner}  ·  binary {binary.binary} [{binary.source}]"
    if launch_profile.env:
        # Name the KEYS with their provenance, not the values: values are paths
        # and tokens, and a banner meant for a terminal should not be where a
        # credential path first becomes shoulder-readable. The full command
        # below carries the values for anyone who wants to paste it.
        shown = ", ".join(
            f"{key} [{launch_profile.env_sources.get(key, 'config')}]"
            for key in sorted(launch_profile.env)
        )
        banner = f"{banner}  ·  env {shown}"
    command = " ".join([*env_assignments, shlex.join(argv)])
    if tracing is not None and tracing.enabled:
        # Never burn a pipeline id into a printable command: every paste would
        # reuse the same id and those sessions would merge into one Run. The
        # eval mints a fresh id per run instead; if tracing is down at run
        # time, the substitution comes back empty with the reason on stderr
        # and the session starts untraced — the same fail-open as --exec.
        #
        # That same eval also exports the id it minted, so the agent can be
        # STARTED on it and its board row joins the Run (the correlation
        # spine). The clear-out leads because what a previous paste exported
        # outlives it — see _CLEAR_PREVIOUS_TRACE for the merge it prevents.
        if explainability_service.accepts_session_id(binary.binary):
            command = f"{command} {_SESSION_ID_SUBSTITUTION}"
        command = (
            f"{_CLEAR_PREVIOUS_TRACE}; "
            f'eval "$(aisquare explainability env {shlex.quote(role_name)})"; {command}'
        )
    if get_state().json_output:
        typer.echo(
            json.dumps(
                {
                    "role": role_name,
                    "model": resolution.model if resolution else None,
                    "effort": resolution.effort if resolution else None,
                    "source": resolution.source if resolution else "untiered",
                    "effort_source": resolution.effort_source if resolution else None,
                    "skipped": resolution.skipped if resolution else [],
                    "binary": binary.binary,
                    "binary_source": binary.source,
                    "env": launch_profile.env,
                    "env_sources": launch_profile.env_sources,
                    "extra_args": launch_profile.args,
                    "command": command,
                }
            )
        )
        if not execute:
            return
    else:
        stdout_console().print(f"✓ {banner}", markup=False)
        if resolution is not None:
            caution = harness.effort_warning(resolution.model, resolution.effort)
            if caution:
                stdout_console().print(f"  ⚠ {caution}", markup=False)
    if execute:
        if shutil.which(binary.binary) is None:
            # Names the candidate AND where it came from: a bare "not found"
            # sends the reader hunting through flag, env and config to learn
            # which of them chose it. Never falls back to the default — that
            # would run the WRONG agent under the right role name.
            fail(
                f"{binary.binary!r} not found on PATH (chosen by: {binary.source}). "
                # No square brackets in this string: the console renders rich
                # markup, so a TOML table name written "[team.profiles]" is
                # swallowed as a tag and the third remedy vanishes from the
                # message that exists to list all three. Naming the command
                # that writes it avoids the problem and is the better hint.
                f"Set a different one with --bin, {_bin_env_hint(role_name)}, or "
                f"aisquare team bind {role_name} --bin <command>.",
                error="agent_binary_missing",
            )
        env = dict(os.environ)
        env["AISQUARE_ROLE"] = role_name
        # The operator's variables, carried verbatim. We do not validate them:
        # this layer has no idea what any given key means, and guessing which
        # ones name a directory that ought to exist is exactly the coupling
        # this design removes.
        env.update(launch_profile.env)
        if tracing is not None and tracing.enabled:
            # Same seam as ``aisquare launch``: wire_session fails open, so a
            # dead or wrong proxy costs the trace, never the spawn. The id is
            # planned from the args this spawn will really run with — a role
            # whose profile already carries --session-id or --resume owns its
            # own id and must not be handed a second one.
            # Disown a parent's identity before wiring, for the reason spelled
            # out in `aisquare launch`: a spawn from inside a traced session
            # would otherwise inherit that session's Run, or be stood down and
            # launch untraced. The operator's own gateway carries no marker,
            # so it survives this untouched.
            parent_run = explainability_service.disown_inherited_trace(env)
            if parent_run:
                typer.echo(
                    f"explainability: spawned from a session traced as {parent_run} — "
                    "this one takes its own identity",
                    err=True,
                )
            identity = explainability_service.plan_session_identity(
                binary.binary, launch_profile.args
            )
            wiring = explainability_service.wire_session(
                explainability_ops.effective_settings(tracing),
                role_name,
                session_id=identity.session_id,
                base_env=env,
            )
            env.update(wiring.env)
            typer.echo(f"explainability: {wiring.reason}", err=True)
            if wiring.traced:
                # Pinned only on a spawn that is really traced: an untraced one
                # has no Run to join, so touching its argv would be risk with
                # no correlation to show for it.
                argv = [*argv, *identity.inject_args]
                # Same marker `aisquare launch` sets: it tells a spawn command
                # run from INSIDE this session that the ANTHROPIC_* it can see
                # are ours to clear, not the operator's own gateway — and it
                # is what the hook inside the agent reads to record the join.
                env.update(explainability_service.trace_marker(wiring))

        os.execvpe(argv[0], argv, env)
    if not get_state().json_output:
        stdout_console().print(f"  run it in the role's terminal:\n  {command}", markup=False)


@app.command("harness")
def harness_status() -> None:
    """Show the role→model matrix and how each ladder resolves right now."""
    if not orchestrator.team_enabled():
        _fail_team(TeamDisabledError())
    rows = []
    base, base_source = harness.base_effort()
    for name, profile in harness.ROLE_PROFILES.items():
        resolution = harness.resolve_model(name, probe=False)
        assert resolution is not None  # every profiled role resolves
        pinned = harness.role_model_override(name)
        rows.append(
            {
                "role": name,
                "ladder": profile.ladder,
                "effort_offset": profile.effort_offset,
                "effort": resolution.effort,
                "effort_source": resolution.effort_source,
                "resolves_to": pinned or resolution.model,
                "source": "pinned" if pinned else resolution.source,
                "mission": profile.mission,
                "binary": harness.resolve_binary(name).binary,
                "binary_source": harness.resolve_binary(name).source,
                "env": harness.resolve_profile(name).env,
                "extra_args": harness.resolve_profile(name).args,
            }
        )
    interference = harness.interfering_env()
    if get_state().json_output:
        typer.echo(
            json.dumps(
                {
                    "base_effort": base,
                    "base_effort_source": base_source,
                    "roles": rows,
                    "interfering_env": interference,
                }
            )
        )
        return
    console = stdout_console()
    console.print(f"base effort: {base} ({base_source})", markup=False)
    for name, profile in harness.ROLE_PROFILES.items():
        resolution = harness.resolve_model(name, probe=False)
        assert resolution is not None  # every profiled role resolves
        pinned = harness.role_model_override(name)
        ladder = "→".join(profile.ladder)
        offset = f"+{profile.effort_offset}" if profile.effort_offset else " 0"
        source = "pinned" if pinned else resolution.source
        binary = harness.resolve_binary(name)
        # The matrix showed WHAT each role runs on and hid WHICH executable
        # runs it — half the launch decision, invisible.
        bin_note = "" if binary.source == "default" else f"  bin={binary.binary} [{binary.source}]"
        launch_profile = harness.resolve_profile(name)
        # ...and hid the env the role carries, which is the axis a
        # parallel-install operator actually steers by. Keys only: the values
        # are paths and tokens and this is a terminal.
        if not launch_profile.is_empty:
            carried = ",".join(sorted(launch_profile.env))
            extra = f"+{len(launch_profile.args)}args" if launch_profile.args else ""
            bin_note = f"{bin_note}  env={carried}{extra}"
        console.print(
            f"{name:<10} {ladder:<20} effort={resolution.effort:<10}({offset}) "
            f"→ {pinned or resolution.model} [{source}]{bin_note}",
            markup=False,
        )
    if interference:
        console.print(f"⚠ env overrides model selection: {', '.join(interference)}", markup=False)
    console.print(
        "Resolution shown without probing — `aisquare team spawn <role>` verifies live.",
        markup=False,
    )


@app.command("bind")
def bind(
    role_name: Annotated[
        str | None,
        typer.Argument(help="Role to pin. Omit to show the current bindings."),
    ] = None,
    agent_bin: Annotated[
        str | None,
        typer.Option("--bin", help="Executable this role launches, e.g. a wrapper script."),
    ] = None,
    env_pairs: Annotated[
        list[str] | None,
        typer.Option(
            "--env",
            "-e",
            help="KEY=VALUE this role launches with (repeatable). Values may use ~ and "
            "$VAR, expanded at launch. Merges per key with what is already bound.",
            metavar="KEY=VALUE",
        ),
    ] = None,
    extra_args: Annotated[
        list[str] | None,
        typer.Option("--arg", help="Extra argument this role launches with (repeatable)."),
    ] = None,
    unset: Annotated[
        list[str] | None,
        typer.Option(
            "--unset", help="Remove one env key from this role (repeatable).", metavar="KEY"
        ),
    ] = None,
    clear: Annotated[
        bool,
        typer.Option("--clear", help="Remove this role's binding entirely."),
    ] = False,
) -> None:
    """Pin a role's launch spec — binary, env and extra args — persistently.

    The one-time setup behind ``team.profiles``: bind a seat once, and every
    later ``aisquare launch <role>`` and ``aisquare team spawn <role>`` carries
    it with no flag. Flags still win over the binding, so any seat can be
    changed for a single launch without touching config.

    Nothing here interprets what you bind. Parallel agent installs reached
    through shell aliases are just two env entries::

        aisquare team bind coder1 \\
          --env CLAUDE_CONFIG_DIR='$HOME/.claude2' \\
          --env CLAUDE_CODE_TMPDIR='$HOME/.cache/claude2'

    ...and a proxy, a region or a wrapper's own variables work identically.
    Quote values containing ``$`` so your shell does not expand them first —
    expansion happens at launch, which is what lets one binding follow you
    across machines with different homes.

    Called with no role it prints the bindings.
    """
    if role_name is None:
        _show_bindings()
        return
    if clear:
        with expected_config_write_errors():
            settings_service.clear_role_binding(role_name)
        bound = None
    else:
        if not any((agent_bin, env_pairs, extra_args, unset)):
            fail(
                "nothing to bind — pass --bin, --env, --arg, --unset or --clear",
                error="nothing_to_bind",
            )
        with expected_config_write_errors():
            bound = settings_service.bind_role(
                role_name,
                agent_bin=agent_bin,
                env=_parse_env(env_pairs or []),
                unset=unset or [],
                args=extra_args or [],
            )
    path = settings_service.config_path()
    if get_state().json_output:
        typer.echo(
            json.dumps(
                {
                    "role": role_name,
                    "profile": bound.model_dump(mode="json") if bound else None,
                    "config": str(path),
                }
            )
        )
        return
    console = stdout_console()
    if bound is None:
        console.print(f"✓ {role_name}: binding cleared ({path})", markup=False)
        return
    console.print(f"✓ {role_name}: {_describe(bound) or '(empty)'} ({path})", markup=False)


def _describe(profile: RoleLaunchProfile) -> str:
    """One line for a binding. Values are shown VERBATIM, not expanded — this
    view is for editing, and resolving `$HOME` here would hide the very thing
    that makes a binding portable. `team harness` shows what it resolves to."""
    parts = [
        f"bin={profile.bin}" if profile.bin else "",
        "  ".join(f"{key}={value}" for key, value in sorted(profile.env.items())),
        f"args={shlex.join(profile.args)}" if profile.args else "",
    ]
    return "  ".join(part for part in parts if part)


def _show_bindings() -> None:
    """Print every role's binding, or how to make one."""
    profiles = settings_service.role_bindings()
    if get_state().json_output:
        typer.echo(
            json.dumps({"profiles": {r: p.model_dump(mode="json") for r, p in profiles.items()}})
        )
        return
    console = stdout_console()
    if not profiles:
        console.print(
            "no role bindings — pin one with: aisquare team bind coder1 "
            "--env CLAUDE_CONFIG_DIR='$HOME/.claude2'",
            markup=False,
        )
        return
    for name in sorted(profiles):
        console.print(f"{name:<12} {_describe(profiles[name]) or '(empty)'}", markup=False)


def warn_board_scope(as_session: str | None) -> None:
    """Say which board a cwd-resolved read answered for, when it may not be yours.

    STDERR, always — a note on stdout would corrupt `--json` output that gets
    piped into jq, which is exactly how these commands are used when someone is
    debugging the thing this note exists for.

    Silent when `--as` was given: that routes board resolution through the
    session's own row (`log_events`, "exactly like attributed writes (#20)"),
    so the answer is already anchored to something other than the directory.
    """
    if as_session is not None:
        return
    note = team_service.board_scope_note()
    if note:
        typer.echo(note, err=True)


@app.command("log")
def log(
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many events to show.")] = 30,
    by: Annotated[
        str | None, typer.Option("--by", help="Only events by this session (id prefix).")
    ] = None,
    mine: Annotated[
        bool, typer.Option("--mine", help="Only events by the --as session (self-check).")
    ] = False,
    since: Annotated[
        str | None, typer.Option("--since", help="Only events after: 15m, 2h, or an ISO time.")
    ] = None,
    since_seq: Annotated[
        int | None, typer.Option("--since-seq", help="Only events past this seq (cursor).")
    ] = None,
    kind: Annotated[
        str | None, typer.Option("--kind", help="Only this kind (note, decision, task_done, …).")
    ] = None,
    task: Annotated[
        str | None, typer.Option("--task", help="Only events about this task (id prefix).")
    ] = None,
    as_session: SessionRef = None,
) -> None:
    """Show the team pipe: recent events from every session, filterable."""
    if mine and by is not None:
        raise typer.BadParameter("--mine and --by are mutually exclusive")
    if mine:
        if as_session is None:
            fail("--mine needs --as <session> (your id is on the board)", error="missing_session")
        by = as_session
    moment = _parse_since(since) if since is not None else None
    try:
        events = team_service.log_events(
            limit=limit,
            by=by,
            since=moment,
            since_seq=since_seq,
            kind=kind,
            task_ref=task,
            session_ref=as_session,
        )
    except STORE_ERRORS as exc:
        _fail_team(exc, task or by or as_session)
    warn_board_scope(as_session)
    if get_state().json_output:
        typer.echo(json.dumps([event.as_envelope().model_dump(mode="json") for event in events]))
        return
    if not events:
        stdout_console().print("No team events match.")
        return
    console = stdout_console()
    for event in events:
        who = team_service.short_id(event.session_id) if event.session_id else "cli"
        console.print(
            f"{local_time(event.created_at):%H:%M} {who} {event.kind}: {event.text}",
            markup=False,
        )


@app.command("verify")
def verify(
    receipt: Annotated[
        str, typer.Argument(help="A write receipt: event seq number, or event id (prefix ok).")
    ],
    as_session: SessionRef = None,
) -> None:
    """Re-check a receipt: prove the write is really on this board.

    The pull side of delivery trust — every ✓ prints ``seq N on <board>``
    (#20's push side); this re-proves it any time, from any process.
    """
    try:
        result = team_service.verify_receipt(receipt, session_ref=as_session)
    except STORE_ERRORS as exc:
        _fail_team(exc, receipt)
    if result.event is None:
        aside = f" — it exists on board {result.elsewhere}" if result.elsewhere else ""
        fail(
            f"no event matches receipt '{receipt}' on board {result.board_name}{aside}",
            error="not_found",
            ref=receipt,
            hint=result.elsewhere,
        )
    if get_state().json_output:
        payload = result.event.as_envelope().model_dump(mode="json")
        payload["delivered"] = True
        typer.echo(json.dumps(payload))
    else:
        stdout_console().print(
            f"✓ delivered · seq {result.event.seq} on {result.board_id}: {result.line}",
            markup=False,
        )


def _signal_json(state: team_service.SignalState) -> dict[str, object]:
    return {
        "name": state.name,
        "value": state.value,
        "set_by": state.set_by,
        "seq": state.seq,
        "updated_at": state.updated_at.isoformat() if state.updated_at else None,
    }


def _signal_line(state: team_service.SignalState) -> str:
    who = team_service.short_id(state.set_by) if state.set_by else "cli"
    when = f" at {local_time(state.updated_at):%H:%M}" if state.updated_at else ""
    return f"{state.name} = {state.value} · set by {who}{when} · seq {state.seq}"


@app.command("signal")
def signal(
    name: Annotated[str, typer.Argument(help="Signal name (lowercase token, e.g. fold-ready).")],
    value: Annotated[
        str | None,
        typer.Argument(help="New value (single token). Omit to read the current value."),
    ] = None,
    as_session: SessionRef = None,
) -> None:
    """Set or read a named board state — structured, never substring-matched.

    ``team signal fold-ready on --as <sid>`` sets; ``team signal fold-ready``
    reads. Every set emits a ``signal`` event whose payload carries
    ``name``/``value``/``prev``/``set_by`` — watchers key on fields, so a
    note saying "NOT READY" can never trip a ``ready`` watcher again (#23).
    """
    if value is None:
        try:
            state = team_service.read_signal(name, session_ref=as_session)
        except STORE_ERRORS as exc:
            _fail_team(exc, as_session)
        if state is None:
            fail(f"no signal named '{name}' on this board", error="not_found", ref=name)
        if get_state().json_output:
            typer.echo(json.dumps(_signal_json(state)))
        else:
            stdout_console().print(_signal_line(state), markup=False)
        return
    try:
        state, prev = team_service.set_signal(name, value, session_ref=as_session)
    except ValueError as exc:
        fail(str(exc), error="invalid_signal", ref=name)
    except STORE_ERRORS as exc:
        _fail_team(exc, as_session)
    delivery = team_service.last_delivery()
    emit_write_warning(delivery)
    if get_state().json_output:
        payload = _signal_json(state)
        payload["prev"] = prev
        payload.update(delivery_fields(delivery))
        typer.echo(json.dumps(payload))
    else:
        was = f" (was {prev})" if prev is not None else ""
        stdout_console().print(
            f"✓ signal {state.name}: {state.value}{was}{receipt_suffix(delivery)}",
            markup=False,
        )


@app.command("signals")
def signals(as_session: SessionRef = None) -> None:
    """List every named board state and who set it."""
    try:
        states = team_service.list_signals(session_ref=as_session)
    except STORE_ERRORS as exc:
        _fail_team(exc, as_session)
    if get_state().json_output:
        typer.echo(json.dumps([_signal_json(state) for state in states]))
        return
    if not states:
        stdout_console().print("No signals set. Set one with: aisquare team signal <name> <value>")
        return
    console = stdout_console()
    for state in states:
        console.print(_signal_line(state), markup=False)


def _fmt_idle(minutes: int) -> str:
    """Render an idle span the way the board's ``_age`` does (12m, 3h07m)."""
    return f"{minutes}m" if minutes < 60 else f"{minutes // 60}h{minutes % 60:02d}m"


@app.command("prune")
def prune(
    older_than: Annotated[
        int | None,
        typer.Option(
            "--older-than",
            help="Minutes without a heartbeat before a session counts as a ghost "
            "(default: 30, the board's stale mark).",
        ),
    ] = None,
    keep: Annotated[
        str | None,
        typer.Option("--keep", help="Spare this session (id prefix) even if it looks stale."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show who would be retired without touching anything."),
    ] = False,
    release_claims: Annotated[
        bool,
        typer.Option(
            "--release-claims",
            help="Also return in-progress claims to the pool at the presence threshold. "
            "Only for sessions you know are dead — by default claims wait 4h, because "
            "30 minutes of agent silence is usually one long tool call.",
        ),
    ] = False,
) -> None:
    """Retire ghost sessions — a clean roll-call.

    A dead loop or crashed terminal lingers on the board as ``(stale)`` forever.
    This ends those rows so the board shows who is actually here.

    **Presence and claims retire on different clocks.** The row goes at the
    threshold (30m); a session's in-progress CLAIMS are only freed after 4h of
    silence, because for an agent thirty minutes of silence is usually one long
    tool call, and a claim released under a working agent hands its lane to a
    second one. ``--release-claims`` frees them at the presence threshold when
    you know the sessions are dead. Data-safe either way: only presence +
    orphaned claims change — tasks, notes, events and the brain are untouched.
    """
    try:
        report = team_service.prune_sessions(
            older_than, dry_run=dry_run, keep=keep, release_claims=release_claims
        )
    except STORE_ERRORS as exc:
        _fail_team(exc, keep)
    delivery = team_service.last_delivery()
    if get_state().json_output:
        payload: dict[str, object] = {
            "dry_run": report.dry_run,
            "threshold_minutes": report.threshold_minutes,
            "released_total": report.released_total,
            "pruned": [
                {
                    "id": p.id,
                    "role": p.role,
                    "idle_minutes": p.idle_minutes,
                    "released": p.released,
                }
                for p in report.pruned
            ],
        }
        payload.update(delivery_fields(delivery))
        typer.echo(json.dumps(payload))
        return
    console = stdout_console()
    if not report.pruned:
        console.print(
            f"✓ roll-call clean — every live session checked in within "
            f"{report.threshold_minutes}m. No ghosts to retire."
        )
        return
    for entry in report.pruned:
        claims = (
            f", freed {entry.released} claim{'' if entry.released == 1 else 's'}"
            if entry.released
            else ""
        )
        bullet = "·" if report.dry_run else "✓"
        console.print(
            f"  {bullet} {team_service.short_id(entry.id)} ({entry.role}) — "
            f"dark {_fmt_idle(entry.idle_minutes)}{claims}",
            markup=False,
        )
    count = len(report.pruned)
    plural = "" if count == 1 else "s"
    if report.dry_run:
        console.print(
            f"— would retire {count} ghost session{plural} (dry run, nothing changed). "
            "Re-run without --dry-run to clear them."
        )
    else:
        tail = (
            f" and returned {report.released_total} orphaned "
            f"claim{'' if report.released_total == 1 else 's'} to the pool"
            if report.released_total
            else ""
        )
        console.print(
            f"🧹 retired {count} ghost session{plural}{tail} — "
            f"board's aligned.{receipt_suffix(delivery)}"
        )


@app.command("distill")
def distill(
    rescan: Annotated[
        bool,
        typer.Option(
            "--all", help="Backfill: re-distill the whole pipe from the beginning (idempotent)."
        ),
    ] = False,
) -> None:
    """Push undistilled notes/decisions/outcomes into the project brain now."""
    try:
        count = team_service.distill_now(rescan=rescan)
    except STORE_ERRORS as exc:
        _fail_team(exc)
    if get_state().json_output:
        typer.echo(json.dumps({"distilled": count}))
    elif count is None:
        stdout_console().print("… another distill is already running — it has the brain")
    else:
        noun = "page" if count == 1 else "pages"
        stdout_console().print(f"✓ distilled {count} {noun} into the project brain")


def recall(
    query: Annotated[str, typer.Argument(help="What to look up in the project brain.")],
) -> None:
    """Search the team's long-term memory (decisions, results, outcomes)."""
    try:
        output = team_service.recall(query)
    except STORE_ERRORS as exc:
        _fail_team(exc)
    if output is None:
        fail(
            "project brain unavailable — gbrain missing, brain busy, or nothing "
            "distilled yet (try `aisquare team distill`)",
            error="brain_unavailable",
        )
    if get_state().json_output:
        typer.echo(json.dumps({"query": query, "output": output}))
    else:
        stdout_console().print(output.rstrip("\n"), markup=False)


def note(
    text: Annotated[str, typer.Argument(help="The note/decision/question/result to share.")],
    as_session: SessionRef = None,
    task: Annotated[
        str | None, typer.Option("--task", help="Task this note is about (id prefix).")
    ] = None,
    to: Annotated[
        str | None, typer.Option("--to", help="Address a role (planner/coder/runner/…).")
    ] = None,
    kind: Annotated[
        str, typer.Option("--kind", help="note, decision, question or result.")
    ] = "note",
) -> None:
    """Share a note with the team (it reaches every session automatically)."""
    try:
        event = team_service.add_note(
            text, session_ref=as_session, task_ref=task, to_role=to, kind=kind
        )
    except ValueError as exc:
        fail(str(exc), error="invalid_task", ref=task)
    except STORE_ERRORS as exc:
        _fail_team(exc, task or as_session)
    delivery = team_service.last_delivery()
    emit_write_warning(delivery)
    if get_state().json_output:
        payload = event.as_envelope().model_dump(mode="json")
        payload.update(delivery_fields(delivery))
        typer.echo(json.dumps(payload))
    else:
        stdout_console().print(
            f"✓ shared ({event.kind}): {event.text}{receipt_suffix(delivery)}", markup=False
        )


def board(
    watch: Annotated[
        bool, typer.Option("--watch", "-w", help="Full-screen live board; Ctrl-C exits.")
    ] = False,
    interval: Annotated[
        float, typer.Option("--interval", "-i", help="Refresh seconds in watch mode.")
    ] = 3.0,
) -> None:
    """Show the live team board (sessions, tasks, recent updates)."""
    if watch:
        if get_state().json_output:
            raise typer.BadParameter("--watch and --json cannot be combined")
        _watch_board(max(interval, 0.5))
        return
    try:
        project, sessions, tasks, events = team_service.board_data()
    except STORE_ERRORS as exc:
        _fail_team(exc)
    warn_board_scope(None)
    if get_state().json_output:
        typer.echo(
            json.dumps(
                {
                    "project": project.model_dump(mode="json"),
                    "sessions": [s.model_dump(mode="json") for s in sessions],
                    "tasks": [t.model_dump(mode="json") for t in tasks],
                    "events": [e.as_envelope().model_dump(mode="json") for e in events],
                }
            )
        )
        return
    if not sessions and not tasks:
        stdout_console().print(
            "The orchestrator is quiet here. Activate with `aisquare team on`, or start a "
            "session with `aisquare launch planner` (coder/runner)."
        )
        return
    stdout_console().print(
        team_service.render_board(project, sessions, tasks, events), markup=False
    )


def _watch_board(interval: float) -> None:
    """Run the live board (interactive TUI or Rich fallback — see cli.watch)."""
    from aisquare.cli import watch as watch_ui

    try:
        watch_ui.run_watch(interval)
    except TeamDisabledError as exc:
        _fail_team(exc)
