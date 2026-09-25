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

One exception, and only when tracing is on AND actually succeeded: the launch
appends ``--session-id <uuid>`` so the agent's session id, the board row and
the gateway Run's ``X-Pipeline-Id`` are one key (see
``services.explainability``). With tracing off — the default — the argv is
byte-identical to what it always was.

The environment has one more decision in it since #145: WHICH CLAUDE ACCOUNT.
``--account`` names one for this launch; without it the role's binding, the
project's default and the machine's default are consulted in that order
(``aisquare accounts default``), and with none of those set the environment is
exactly what it always was. One resolver, ``services.claude_accounts.choose``,
answers for ``launch`` and ``fleet spawn`` alike.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import sqlite3
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Annotated

import typer
from rich.text import Text

from aisquare.cli.common import fail
from aisquare.core import claude_accounts as claude_accounts_core
from aisquare.core import harness, orchestrator, paths, personas
from aisquare.core.config import load_config
from aisquare.core.console import stderr_console
from aisquare.core.store import ContextStore, is_locked_error, store_session
from aisquare.core.workspace import git_common_root
from aisquare.models import ProjectInfo
from aisquare.services import claude_accounts as claude_accounts_service
from aisquare.services import explainability as explainability_service
from aisquare.services import explainability_ops
from aisquare.services import team as team_service
from aisquare.services.team import TeamDisabledError

ROLES = ("planner", "coder", "runner", "tester", "reviewer", "validator", "manager", "ui-tester")
"""Roles with a standing work cycle the orchestrator injects on every prompt.

``tester``, ``reviewer`` and ``manager`` are the fleet's roles
(docs/plans/fleet-tui.md §3.3); ``tester`` shares ``runner``'s cycle.
"""

#: A numbered SEAT of a first-class role — ``coder1``, ``coder2``. Crews run
#: several agents in the same role and need to tell them apart on the board;
#: the work cycle is the role's, so the number is an identity, not a new role.
#:
#: The seat is exported VERBATIM as ``AISQUARE_ROLE`` below, because that is what
#: makes it an identity: the board row says ``coder1``, and ``team bind coder1``
#: binds that seat's own binary and env. What turns the seat back into a role is
#: ``services.team.base_role``, which every harness lookup keyed on a board role
#: goes through — that is where the "the work cycle is the role's" half of this
#: comment is actually kept, and it was NOT kept until it existed: measured,
#: ``harness.role_cycle('coder1', …)`` returned ``[]`` and
#: ``harness.model_mismatch('coder1', …)`` returned ``None``, so a seat launched
#: with no standing cycle and off every ladder.
_SEAT = re.compile(rf"^({'|'.join(ROLES)})\d+$")

DEFAULT_AGENT = "claude"

FLEET_ROW_TIMEOUT = 10.0
"""Seconds of LOOKS a fleet launch spends on its row before starting the agent anyway.

Twice the store's default busy timeout (``_DEFAULT_BUSY_MS``): the spawn's
insert waits that long on a locked ``context.db`` before it fails, and a spawn
that fails kills this window, so a wait past the timeout is one that was never
going to be answered. Not a cap on the wall clock: the one open of the store
retries a locked database for the store's own budget (three times the busy
timeout, 15 s at the default) before this clock is consulted, so a launch on a
locked store can hold longer than this — the line printed names the time it
actually took (review of #203, round 4)."""
FLEET_ROW_POLL = 0.05
"""Seconds between looks for the row — one store read each, and rarely more
than one: the row lands while this interpreter is still starting."""
_sleep: Callable[[float], None] = time.sleep
_monotonic: Callable[[], float] = time.monotonic


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


def _check_persona(name: str, project: ProjectInfo | None) -> None:
    """Refuse a persona this project cannot resolve, before anything starts (§3.7).

    The project's root is the board's; with no board row (an unreadable store) it
    is the git repository around the working directory, as ``aisquare persona``
    reads it.
    """
    root = project.root if project is not None else git_common_root(Path.cwd())
    try:
        personas.resolve(name, root)
    except personas.PersonaError as exc:
        fail(str(exc), error="unknown_persona", ref=name)


SYSTEM_PROMPT_FLAG = "--append-system-prompt-file"
"""Claude Code's seam for appending to its default system prompt — a FILE, so the body
never sits in argv, which ``ps`` shows (the name travels as ``AISQUARE_PERSONA``)."""
_SYSTEM_PROMPT_FLAGS = (SYSTEM_PROMPT_FLAG, "--append-system-prompt")


def _persona_system_prompt(
    name: str, project: ProjectInfo | None, binary: str, args: Sequence[str]
) -> list[str]:
    """The persona in the agent's system prompt too, where the binary has a seam for it.

    Claude Code takes ``--append-system-prompt-file``: the same block the
    session-start hook briefs (``personas.briefing``), written under the home
    and appended to the default system prompt, so the persona holds over a
    long session as hook context alone may not (docs/plans/spawn-personas.md
    §9; gh #210). The hook keeps briefing it: that channel survives ``/clear``,
    this one does not. ``[persona] system_prompt = false`` keeps the hook
    alone. Any other binary — codex, aider, a wrapper not named claude — has no
    seam this launcher knows (codex reads AGENTS.md, aider its own prompts), so
    it gets the hook alone and one dim line says so. An ``--append-system-prompt``
    of either spelling already in the arguments is the operator's and wins.

    The file is named by the block's own hash: two launches of one persona
    share it, and a project-layer persona that differs from a bundled one of
    the same name gets a file of its own — no launch can read another's.
    Owner decision, 2026-09-24.
    """
    try:
        wanted = load_config().persona.system_prompt
    except Exception:  # an unreadable config was already said above; the default stands
        wanted = True
    if not wanted:
        return []
    if any(
        arg == flag or arg.startswith(f"{flag}=") for arg in args for flag in _SYSTEM_PROMPT_FLAGS
    ):
        return []
    if not harness.is_default_agent(binary):
        stderr_console().print(
            f"persona {name}: {os.path.basename(binary)!r} takes no system-prompt flag this "
            "launcher knows — briefed by the session-start hook alone",
            style="dim",
        )
        return []
    root = project.root if project is not None else git_common_root(Path.cwd())
    block = "\n".join(personas.briefing(personas.resolve(name, root))) + "\n"
    digest = hashlib.sha256(block.encode("utf-8")).hexdigest()[:12]
    path = paths.aisquare_home() / "cache" / "persona-prompts" / f"{name}-{digest}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text(encoding="utf-8") != block:
        path.write_text(block, encoding="utf-8")
    return [SYSTEM_PROMPT_FLAG, str(path)]


def _exec(binary: str, argv: list[str], env: dict[str, str]) -> None:
    """Replace this process with the agent (indirection so tests can intercept)."""
    os.execve(binary, argv, env)


def _await_fleet_row() -> None:
    """Under a fleet window, wait for the row ``AISQUARE_FLEET_AGENT`` names to exist.

    ``fleet spawn`` starts the window and writes the row after — the row
    carries the window's pane id, and a label or cap race is settled against
    a window that exists — so the agent's ``SessionStart`` hook could fire
    before the insert committed: a slow or locked ``context.db``, a relabel
    retry, the cap's live-list read. The hook then found no row and briefed
    the agent on nothing, which is the very bug the assignment exists to fix
    (review of #135, second round). This process runs in the window BEFORE
    the agent, so it is the one place that can hold the door: ordinarily the
    row is there on the first look, while this interpreter is still warming
    up. Fail-open at the timeout and on a store that cannot be read — the
    hook fails open the same way — with one line saying what it cost, on
    BOTH exits: an agent that starts un-briefed over a corrupt store with
    nothing on stderr is the state the line exists to explain (review of
    #203, round 4).

    ONE connection for the whole wait. Opening a store connects, switches the
    journal mode and runs the migrations, and the first cut did that on every
    50 ms look — up to two hundred opens, each contending on ``context.db``
    with the very insert this loop waits for — and gave up on the first
    "database is locked", the one condition it exists to wait out (review of
    #203). A lock, on the open or on a look, is looked past until the deadline;
    any other failure of the store ends the wait, never the launch. The
    deadline bounds the looks: one open that is itself waiting out a lock holds
    for the store's own retry budget first (:data:`FLEET_ROW_TIMEOUT`).
    """
    agent_id = orchestrator.env_fleet_agent()
    if agent_id is None:
        return
    started = _monotonic()
    deadline = started + FLEET_ROW_TIMEOUT
    with contextlib.ExitStack() as stack:
        store: ContextStore | None = None
        while True:
            try:
                if store is None:
                    store = stack.enter_context(store_session())
                if store.get_fleet_agent(agent_id) is not None:
                    return
            except sqlite3.OperationalError as exc:
                if not is_locked_error(exc):
                    _starting_unbriefed(agent_id, f"the store could not be read ({exc})")
                    return
            except Exception as exc:  # an unreadable store costs the wait, never the launch
                _starting_unbriefed(
                    agent_id, f"the store could not be read ({type(exc).__name__}: {exc})"
                )
                return
            if _monotonic() >= deadline:
                _starting_unbriefed(agent_id, f"not recorded after {_monotonic() - started:.0f}s")
                return
            _sleep(FLEET_ROW_POLL)


def _starting_unbriefed(agent_id: str, why: str) -> None:
    """The one line every fail-open exit of :func:`_await_fleet_row` prints."""
    stderr_console().print(
        f"fleet: row {agent_id} {why} — starting anyway; the session-start briefing "
        "may miss its assignment",
        style="dim",
    )


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
    account: Annotated[
        str | None,
        typer.Option(
            "--account",
            "-a",
            help="Claude Code account to run under: a slot number, an alias or the email it "
            "is signed in as (see `aisquare accounts`). Sets CLAUDE_CONFIG_DIR and "
            "CLAUDE_CODE_TMPDIR over the role's binding. Without it: the role's bound "
            "account, then the project default, then the machine default.",
            metavar="ACCOUNT",
        ),
    ] = None,
    persona: Annotated[
        str | None,
        typer.Option(
            "--persona",
            help="Persona this session runs as (see `aisquare persona list`): exported as "
            "AISQUARE_PERSONA and briefed once, at session start.",
            metavar="NAME",
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
    except Exception as exc:  # the board is an observer too: an unreadable
        # context.db must cost the board ROW, never the launch — the same
        # fail-open bar as the config read above and as a dead proxy below.
        # Measured before this existed: a corrupt store raised
        # sqlite3.DatabaseError straight through this call, the agent was never
        # handed control, and the operator got a stack trace. "You turned team
        # off" stays a refusal; "your database is damaged" must not be one.
        project = None
        stderr_console().print(
            f"board: context.db unreadable ({exc}) — launching without a board row",
            style="dim",
        )

    if persona is not None:
        _check_persona(persona, project)

    env = {**os.environ, "AISQUARE_ROLE": role}
    if persona is not None:
        # The name travels, never the body: argv is visible in `ps` and a body
        # can be thousands of characters. The session-start hook resolves it and
        # renders the briefing (docs/plans/spawn-personas.md §3.2).
        env["AISQUARE_PERSONA"] = persona
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
            f"role bindings: config unreadable ({profile.notice}) — launching unbound",
            style="dim",
        )
    env.update(profile.env)
    # WHICH ACCOUNT, decided in exactly one place (#145): the flag, else the
    # role's `team bind --account`, else the project's default, else the
    # machine's — `services.claude_accounts.choose`, pinned by
    # tests/test_one_account_resolver.py so `fleet spawn` cannot disagree with
    # a hand-typed launch. An account wins over the binding's env: the flag or
    # the default names an account this launch is FOR, and the binding's env is
    # the role's standing shape. For slot 1 that means RESTORING this shell's
    # own two variables (or their absence) over whatever the binding set — a
    # launch announced as `[plain claude]` must not run on the binding's other
    # login. When NOTHING chose (no flag, no binding, no default — every
    # machine before #145), the environment is left exactly as it was.
    try:
        choice = claude_accounts_service.choose(account, role=role, project=project)
    except claude_accounts_service.NoSuchAccount as exc:
        fail(str(exc), error="unknown_account", ref=account)
    for note in choice.notes:
        # A skipped rung is otherwise invisible: the launch lands on the next
        # one down and nobody learns why. Same channel and style as the
        # binding and tracing notes around this.
        stderr_console().print(f"accounts: {note}", style="dim")
    if choice.account is not None:
        claude_accounts_core.apply_launch_env(env, choice.account, shell=os.environ)
    whose = f" ({','.join(sorted(profile.env))})" if profile.env else ""
    if choice.account is not None:
        whose += f" [{choice.describe()}]"
    try:
        tracing = load_config().explainability
    except Exception as exc:  # tracing is an observer: a broken config must
        # cost the trace, never the launch — the same fail-open bar as a dead
        # proxy. `aisquare doctor` still reports the config error loudly.
        tracing = None
        stderr_console().print(
            f"explainability: config unreadable ({exc}) — launching untraced",
            style="dim",
        )
    # The role's OWN flags (`RoleProfile.default_args`), resolved before the
    # tracing block because the identity planner below must see every arg the
    # agent will get. ONE precedence rule, shared with `team spawn`: the role's
    # defaults sit after the binding's args, and an explicit flag or its
    # `--no-` opt-out WINS wherever it appears — not because of where these
    # land in argv, but because `role_defaults` stands down when either
    # spelling is already in the args it is given.
    defaults = harness.role_defaults(
        role, binary=resolution.binary, args=[*profile.args, *ctx.args]
    )
    role_args = defaults.args
    for note in defaults.notes:
        # A withheld flag is otherwise invisible: the launch succeeds and the
        # role degrades silently (a ui-tester with no browser reopens every UI
        # task). Same surface and style as the tracing notes below.
        stderr_console().print(f"{role}: {note}", style="dim")
    #: Appended to the agent's argv, and empty unless a trace actually happened.
    pinned_id: list[str] = []
    if tracing is not None and tracing.enabled:
        # Fail-open by contract: wire_session returns an empty env delta (plus
        # the reason) rather than raising, so a dead or wrong proxy can only
        # ever cost the trace, never the launch. Disabled config skips even
        # this block — the default launch stays byte-identical.
        # The EFFECTIVE binary, not the flag: post-#57 `command` is None unless
        # the caller typed --command, and the role's binding decides what runs.
        # Passing the flag here would hand `None` to os.path.basename (a crash,
        # i.e. tracing costing a launch) and would ask "does claude accept
        # --session-id?" about a role bound to a wrapper that does not.
        # Agents spawn agents, and a traced parent's environment carries the
        # wiring that traced it. Keeping it hands this child the PARENT's Run;
        # standing down on it — what happened before this line existed —
        # dropped every agent below the first off the trace entirely. So ours
        # is disowned and the child wires its own. A gateway the operator set
        # up has no marker beside it, is not ours, and still makes us stand
        # down at the reserved-var guard exactly as before.
        parent_run = explainability_service.disown_inherited_trace(env)
        if parent_run:
            stderr_console().print(
                f"explainability: launched from a session traced as {parent_run} — "
                "this one takes its own identity",
                style="dim",
            )
        # The EFFECTIVE argument list, not just what this invocation typed.
        # `argv` below is
        # `[binary, *profile.args, *role_args, *ctx.args, *pinned_id]`, so a
        # role bound with `--session-id`, `--resume` or `--continue` via
        # `team bind --arg` — or handed one by its own `RoleProfile.default_args`
        # — carries it here without appearing in `ctx.args`.
        # Planning on `ctx.args` alone therefore read those launches as fresh:
        # a bound `--session-id X` got a SECOND `--session-id` appended after
        # it, and a bound `--continue`/`--resume` defeated the deliberate
        # refusal to pin — the one this module's own comment calls "pure risk
        # for no correlation", because guessing an id merges two agents onto one
        # board row and one Run. `team spawn` already passes its profile args
        # (cli/team.py), so this path was the asymmetric one.
        identity = explainability_service.plan_session_identity(
            resolution.binary, [*profile.args, *role_args, *ctx.args]
        )
        # The ACTIVE target's overrides folded onto the settings the wiring
        # reads. `explainability enable --target prod --proxy-url …` writes
        # them per target, and wire_session only ever looks at the top level —
        # so without this fold a launch silently uses the wrong proxy while
        # reporting success, which is worse than config that is simply absent.
        # `effective` folds the active target's overrides onto the settings the
        # wiring reads; `api_key` is the key a HOSTED proxy authenticates on,
        # resolved through that same target and never from a hardcoded variable.
        # ONE guard for both, because they have one failure story: a broken
        # target definition costs the overrides and the key — so the trace — and
        # never the launch.
        try:
            # One project for both: its destination may name another target (#142).
            project_id = project.id if project is not None else None
            effective = explainability_ops.effective_settings(tracing, project_id=project_id)
            target = explainability_ops.resolve_target(tracing, project_id=project_id)
            api_key, gateway_url = target.api_key, target.gateway_url
        except Exception as exc:
            effective, api_key, gateway_url = tracing, None, None
            stderr_console().print(
                f"explainability: target unreadable ({exc}) — using the top-level "
                "settings, untraced if that proxy needs a key",
                style="dim",
            )
        wiring = explainability_service.wire_session(
            effective,
            role,
            session_id=identity.session_id,
            base_env=env,
            api_key=api_key,
            gateway_url=gateway_url,
        )
        env.update(wiring.env)
        stderr_console().print(f"explainability: {wiring.reason}", style="dim")
        if wiring.traced:
            # Only pin the id on a launch that is REALLY traced. An untraced
            # launch has no Run to join, so touching its argv would be pure
            # risk for no correlation — and it keeps the fallback identical to
            # the launch the user would have got before any of this existed.
            pinned_id = list(identity.inject_args)
            # Marks the ANTHROPIC_* beside it as OURS rather than the
            # operator's own gateway, which is what lets a spawn command run
            # from inside this session clear them and take its own identity
            # instead of silently inheriting this one's Run.
            # Carried INTO the agent, because the hook that runs inside it is
            # the only place that knows the board session id. That seam records
            # the join for EVERY binary, wrapper or not — which is why nothing
            # here needs to write one, and why an unpinnable launch still joins.
            env.update(explainability_service.trace_marker(wiring))
    # The persona's system-prompt seam, after the role's own flags and before the
    # operator's line: `_persona_system_prompt` stands down when that line already
    # carries the flag. `_check_persona` refused an unknown name above, so the
    # resolve inside cannot fail here.
    persona_args = (
        _persona_system_prompt(
            persona, project, resolution.binary, [*profile.args, *role_args, *ctx.args]
        )
        if persona is not None
        else []
    )
    argv = [resolution.binary, *profile.args, *role_args, *persona_args, *ctx.args, *pinned_id]
    # Text.assemble rather than "[bold]{role}[/bold]": this is the one line that
    # styles a single token instead of the whole line, and it interpolates a
    # role name, a binary path and a project name. A Text carries its styling
    # as structure, so the parser never sees the data at all.
    stderr_console().print(
        Text.assemble(
            f"Launching {resolution.binary}{whose} as ",
            (role, "bold"),
            f" on the {project.root.name or project.id} board…"
            if project is not None
            else " with no board row (context.db unreadable)…",
        )
    )
    _await_fleet_row()
    _exec(binary, argv, env)


def register(app: typer.Typer) -> None:
    """Attach ``launch`` to ``app``, forwarding unknown options to the agent."""
    app.command(
        "launch",
        context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    )(launch)
