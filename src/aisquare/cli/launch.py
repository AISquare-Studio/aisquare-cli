"""``aisquare launch`` — start an agent session with its orchestrator role set.

The documented way to join a session to the board was
``AISQUARE_ROLE=coder claude``: an env-var-prefixed launch, retyped in every
terminal, that silently produces an ordinary unattached session when you
forget it. ``aisquare launch coder`` is the same thing with the footgun
removed — it validates the role, opts the repo in explicitly, then *replaces*
this process with the agent so signals, job control and the TTY behave exactly
as if you had run the agent yourself.

Native arguments follow the AISquare options: ``aisquare launch coder
--model opus`` runs ``claude --model opus``. A native command/prompt or an
explicit ``--`` ends AISquare's option parsing.

One exception, and only when tracing is on AND actually succeeded: the launch
prepends ``--session-id <uuid>`` so the agent's session id, the board row and
the gateway Run's ``X-Pipeline-Id`` are one key (see
``services.explainability``). With tracing off — the default — the argv is
byte-identical to what it always was.

The environment has one more decision in it since #145: WHICH CLAUDE ACCOUNT.
``--account`` names one for this launch; without it the role's binding, the
project's default and the machine's default are consulted in that order
(``aisquare accounts default``), and with none of those set the environment is
exactly what it always was. One resolver, ``services.claude_accounts.choose``,
answers for ``launch`` and ``fleet spawn`` alike — for a Codex role too, where
the account is a Codex slot and the launch sets ``CODEX_HOME`` to it.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Annotated

import typer
from rich.text import Text

from aisquare.cli.common import fail
from aisquare.cli.native_args import NativeForwardingCommand as LaunchCommand
from aisquare.core import claude_accounts as claude_accounts_core
from aisquare.core import experimental, harness, orchestrator, paths, personas
from aisquare.core.agent_adapters.types import BadEffortError
from aisquare.core.config import load_config
from aisquare.core.console import stderr_console
from aisquare.core.store import ContextStore, is_locked_error, store_session
from aisquare.core.workspace import git_common_root
from aisquare.models import ProjectInfo
from aisquare.services import agent_launch, explainability_ops
from aisquare.services import claude_accounts as claude_accounts_service
from aisquare.services import explainability as explainability_service
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

#: launch's own short options, which an agent may spell with a value of its own (Codex's
#: ``-a`` is its approval policy): one the agent's ``capabilities.value_options`` names too
#: is told apart in the refusal of a value that is none of launch's.
_ACCOUNT_SHORT = "-a"

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


UNSEATED = ("captain",)
"""Roles with a standing cycle that never take a numbered seat: the captain is one per HOME
(services.captain, T2), so ``captain1`` is not a seat of anything — and ``ROLES`` stays
the list whose every member has a harness profile (``_SEAT`` is built from it)."""


def _role_ok(role: str) -> bool:
    """First-class role, a numbered seat of one, or declared in config.

    The whitelist earns its keep by catching typos — ``codr`` silently
    producing an unattached session was the original footgun — so this stays a
    check rather than becoming free-form. It just stops rejecting the two
    shapes real crews use: numbered seats, and roles the operator has already
    written down.

    The captain (:data:`UNSEATED`) is a role only while the experimental captain is on.
    Off, it is the unknown role main calls it: ``launch captain`` in a fleet window
    started an agent under a role that does not exist, and ``fleet spawn captain`` named
    the off feature in its refusal (review of #240, F2).
    """
    return (
        role in ROLES
        or (role in UNSEATED and experimental.captain_enabled())
        or bool(_SEAT.match(role))
        or role in _declared_roles()
    )


def _persona_root(project: ProjectInfo | None) -> Path | None:
    """Where a launch resolves its persona: the working directory's own project
    (``orchestrator.directory_project``), the one ``fleet spawn`` checked the name against
    and the session-start hook briefs from, never the board's. Under ``AISQUARE_TEAM_HUB``
    the board is the hub's, and a seat spawned with a persona from its own repository's
    layer died at launch on "no persona named …" (review of #201, F1). With no answer from
    the directory (a symlink loop) it is the board's, else the git repository around it.

    One rule for the check and the system-prompt file both: the file resolved at the
    board's root, so under a hub the launch the check had passed ended in a traceback, or
    wrote the HUB's persona of that name into the system prompt while the hook briefed the
    repository's (review of #240, F1).
    """
    try:
        return orchestrator.directory_project(Path.cwd()).root
    except OSError:
        return project.root if project is not None else git_common_root(Path.cwd())


def _check_persona(name: str, project: ProjectInfo | None) -> None:
    """Refuse a persona this project cannot resolve, before anything starts (§3.7), in the
    layer :func:`_persona_root` names."""
    try:
        personas.resolve(name, _persona_root(project))
    except personas.PersonaError as exc:
        fail(str(exc), error="unknown_persona", ref=name)


def _persona_system_prompt(
    name: str,
    project: ProjectInfo | None,
    selected: agent_launch.ResolvedAgent,
    env: Mapping[str, str],
    args: Sequence[str],
) -> list[str]:
    """The persona in the agent's system prompt too, where its adapter has a seam for it
    (``system_prompt_args``).

    The block is the one the session-start hook briefs (``personas.briefing``), so the two
    channels cannot disagree, and the persona holds over a long session as hook context
    alone may not (docs/plans/spawn-personas.md §9; gh #210). Claude Code takes the block's
    file (``--append-system-prompt-file``); Codex takes its text as ``-c
    developer_instructions=…`` after the operator's own, from the config dir this launch
    runs in. The system prompt is the LAUNCH's, for the life of the process: ``/clear``
    keeps it, and a persona attached since (``persona attach``) reaches the agent through
    the hook alone, which briefs the persona the fleet row names at every session start. A
    restart replays the row's persona, so the two agree again from it on (review of #240),
    except on an agent whose resumed thread keeps its first system prompt
    (``capabilities.resume_keeps_system_prompt``: Codex's). ``[persona] system_prompt =
    false`` keeps the hook alone. An agent with no seam, or a launch whose own arguments
    already set it (the operator's, which wins), gets the hook alone and one dim line
    saying so, as does a cache it cannot write where the seam takes the file.

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
    try:
        persona = personas.resolve(name, _persona_root(project))
    except personas.PersonaError as exc:
        # Checked a moment ago (``_check_persona``), in the same layer: one removed or broken
        # since costs the seam, never the launch, and the hook says what it finds.
        stderr_console().print(
            f"persona {name}: {exc} — no system-prompt file, the session-start hook alone",
            style="dim",
            markup=False,
        )
        return []
    block = "\n".join(personas.briefing(persona)) + "\n"
    digest = hashlib.sha256(block.encode("utf-8")).hexdigest()[:12]
    path = paths.aisquare_home() / "cache" / "persona-prompts" / f"{name}-{digest}.md"
    adapter = selected.adapter
    seam = adapter.system_prompt_args(path, block, args, agent_launch._launch_home(selected, env))
    if seam is None:
        stderr_console().print(
            f"persona {name}: no system-prompt seam for {adapter.label} on this launch (it "
            "has none, or the launch's own arguments set it) — briefed by the session-start "
            "hook alone",
            style="dim",
        )
        return []
    if str(path) not in seam:  # the agent took the text itself
        return seam
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text(encoding="utf-8") != block:
            path.write_text(block, encoding="utf-8")
    except OSError as exc:
        # A cache the launcher cannot write costs the seam, never the launch: the hook
        # still briefs the persona. Unguarded, an unwritable home ended `launch --persona`
        # in a traceback (review of #240, "also confirmed": the persona cache).
        stderr_console().print(
            f"persona {name}: its system-prompt file could not be written ({exc}) — "
            "briefed by the session-start hook alone",
            style="dim",
        )
        return []
    return seam


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
            help="Agent command to launch (-c BINARY; Codex -c KEY=VALUE is forwarded). "
            "Overrides the role's bound `bin`; "
            "defaults to that, then to the selected coding agent.",
            metavar="CMD",
        ),
    ] = None,
    agent: Annotated[str | None, typer.Option("--agent", help="Coding agent family.")] = None,
    bound_args: Annotated[
        bool,
        typer.Option(
            "--bound-args/--no-bound-args",
            help="Use the role's saved native arguments (disable to supply a complete command).",
        ),
    ] = True,
    custom_role: Annotated[
        bool,
        typer.Option("--custom-role", help="Allow an unbound custom role, as team spawn does."),
    ] = False,
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
            help="Account to run under, of the role's agent: a slot number, an alias or the "
            "email it is signed in as (see `aisquare accounts`). Sets CLAUDE_CONFIG_DIR and "
            "CLAUDE_CODE_TMPDIR (Claude Code) or CODEX_HOME (Codex) over the role's binding. "
            "Without it: the role's bound account, then the project default, then the "
            "machine default. -a is this option's, not Codex's --ask-for-approval: spell "
            "that one out, or pass it after --.",
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
    if not custom_role and not _role_ok(role):
        fail(
            f"unknown role {role!r} — expected one of: {', '.join(ROLES)}, "
            "a numbered seat of one (coder1, coder2), or a role you have "
            "bound with `aisquare team bind`",
            error="unknown_role",
        )
    if experimental.captain_role(role) and orchestrator.env_fleet_agent() is None:
        # Only the window `aisquare captain` starts is the captain: its brain folder,
        # its one server, no other tool. By hand in a project this was plain claude
        # with every tool, briefed as the one captain that has no shell.
        fail(
            f"the {role} lives on the home board, one per home — `aisquare captain` starts it",
            error="unknown_role",
        )
    # Resolve WHICH executable on the same ladder `team spawn` uses, so a role
    # bound to a wrapper launches on it here too. This used to read `--command`
    # alone and ignore the binding entirely, which is worse than not supporting
    # it: the docstring promised the profile supplied the binary, so `launch`
    # silently started the DEFAULT agent under the right role name and exited 0.
    try:
        environment = harness.parse_env_pairs(env_pairs or [])
    except ValueError as exc:
        fail(str(exc), error="bad_env_pair")
    try:
        selected = agent_launch.resolve(
            role,
            agent=agent,
            binary=command,
            env_overrides=environment,
        )
    except ValueError as exc:
        fail(str(exc), error="agent_configuration")
    resolution = selected.binary
    binary = agent_launch.executable(selected)
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
    profile = selected.profile if bound_args else selected.profile.model_copy(update={"args": []})
    if profile.notice is not None:
        # No silent fail-soft: unreadable config means this role launches
        # UNBOUND — possibly on a different install than the operator believes.
        stderr_console().print(
            f"role bindings: config unreadable ({profile.notice}) — launching unbound",
            style="dim",
        )
    if profile.refusal is not None:
        # A variable the binding reads from a file that cannot be read: launched without
        # it, a reviewer bound to a token that cannot push runs on this shell's own.
        fail(profile.refusal, error="env_file_unreadable", detail=profile.refusal)
    env.update(profile.env)
    env.update(profile.file_env)
    parent_run = agent_launch.launch_identity(env, selected, project.root if project else None)
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
    # machine before #145), the environment is left exactly as it was. Every
    # rung answers with an account of the role's agent: a Codex role runs on a
    # Codex slot (`CODEX_HOME`), and another agent's account is refused, naming both.
    choice = claude_accounts_service.AccountChoice(account=None, source=None)
    if selected.adapter.accounts is not None:
        try:
            choice = claude_accounts_service.choose(
                account, role=role, project=project, agent=selected.adapter.id
            )
        except claude_accounts_service.NoSuchAccount as exc:
            message = str(exc)
            native = selected.adapter.capabilities.value_options
            if _ACCOUNT_SHORT in native and account is not None and not account.isdigit():
                message += (
                    f". launch's {_ACCOUNT_SHORT} is --account: for {selected.adapter.label}'s "
                    f"own {_ACCOUNT_SHORT}, pass {_ACCOUNT_SHORT} {account} after --"
                )
            fail(message, error="unknown_account", ref=account)
    elif account is not None:
        fail(
            f"--account selects an account, and {selected.adapter.label} has no account slots",
            error="agent_configuration",
        )
    for note in choice.notes:
        # A skipped rung is otherwise invisible: the launch lands on the next
        # one down and nobody learns why. Same channel and style as the
        # binding and tracing notes around this.
        stderr_console().print(f"accounts: {note}", style="dim")
    if choice.account is not None:
        claude_accounts_core.apply_launch_env(env, choice.account, shell=os.environ)
    # The folder's trust from whichever account has it, so the agent does not stop at
    # Claude Code's "trust this folder?" — whose highlighted answer is "No, exit" — for
    # a folder the human already said yes to on another login (`[accounts] share_trust`).
    # Not again for the account a fleet spawn just shared into: it says so on the window.
    settled = env.pop(claude_accounts_service.SHARED_ENV, None)
    if choice.account is None or settled != str(choice.account.slot):
        for note in claude_accounts_service.share_for_launch(
            account=choice.account, env=env, agent=selected.adapter.id
        ):
            stderr_console().print(f"accounts: {note}", style="dim")
    carried = sorted({*profile.env, *profile.file_env})
    whose = f" ({','.join(carried)})" if carried else ""
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
    # Managed role defaults precede saved and forwarded arguments so a native
    # literal separator cannot turn them into prompt text. Explicit opt-outs
    # still win: role_defaults stands down when either spelling is present.
    defaults = selected.adapter.role_defaults(
        role, binary=resolution.binary, args=[*profile.args, *ctx.args]
    )
    role_args = defaults.args
    for note in defaults.notes:
        # A withheld flag is otherwise invisible: the launch succeeds and the
        # role degrades silently (a ui-tester with no browser reopens every UI
        # task). Same surface and style as the tracing notes below.
        stderr_console().print(f"{role}: {note}", style="dim")
    try:
        arguments = agent_launch.prepare_arguments(selected, [*role_args, *profile.args], ctx.args)
        native_model = agent_launch.launch_model_for(selected, role, arguments)
        for message in native_model.notes if native_model else []:
            stderr_console().print(message, markup=False)
        model_args = agent_launch.resolved_model_args(selected, native_model, arguments)
    except BadEffortError as exc:
        fail(str(exc), error="bad_effort")
    except ValueError as exc:
        fail(str(exc), error="agent_configuration")
    #: Managed flags precede native arguments, and are empty unless tracing succeeded.
    pinned_id: list[str] = []
    if tracing is not None and tracing.enabled and selected.adapter.capabilities.model_proxy:
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
        if parent_run:
            stderr_console().print(
                f"explainability: launched from a session traced as {parent_run} — "
                "this one takes its own identity",
                style="dim",
            )
        # Plan from the complete normalized argument list, including saved
        # session/resume flags. The planner ignores literal prompt text.
        identity = explainability_service.plan_session_identity(resolution.binary, arguments.argv)
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
            api_key, gateway_url, key_env = target.api_key, target.gateway_url, target.api_key_env
        except Exception as exc:
            effective, api_key, gateway_url, key_env = tracing, None, None, None
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
            key_env=key_env,
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
    native_trace_args, native_trace_note = agent_launch.telemetry_args(
        selected, env, arguments.argv
    )
    if native_trace_note:
        stderr_console().print(native_trace_note, markup=False)
    # The persona's system-prompt seam, before the role's and the operator's line:
    # `_persona_system_prompt` stands down when that line already sets the seam, and for
    # an agent with none. `_check_persona` refused an unknown name above, so the resolve
    # inside cannot fail here.
    persona_args = (
        _persona_system_prompt(persona, project, selected, env, arguments.argv)
        if persona is not None
        else []
    )
    # The captain's one server is its own (`tools_only_args`, from the fleet): the
    # aisquare server `[agents] mcp` adds to every other launch would sit beside it, a
    # second --mcp-config its --strict-mcp-config loads, a Codex server its off-list
    # never names (review r17 of #113, finding 3).
    aisquare_server = [] if experimental.captain_role(role) else agent_launch.mcp_args(selected)
    argv = [
        resolution.binary,
        *model_args,
        *pinned_id,
        *native_trace_args,
        *agent_launch.fleet_settings_args(selected, env, arguments.argv),
        *aisquare_server,
        *persona_args,
        *arguments.argv,
    ]
    # Last, over the environment as it will be exec'd: the store is read-only in the
    # agent's sandbox, and its board calls go through this launch's broker.
    from aisquare.services.store_broker import LaunchRefused

    try:
        argv, broker_note = agent_launch.start_broker(selected, env, argv)
    except LaunchRefused as exc:
        fail(str(exc), error="sandbox_writes_aisquare")
    if broker_note:
        stderr_console().print(broker_note, markup=False)
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
    agent_launch.prepare_sandbox(selected)
    _await_fleet_row()
    _exec(binary, argv, env)


def register(app: typer.Typer) -> None:
    """Attach ``launch`` to ``app``, forwarding unknown options to the agent."""
    app.command(
        "launch",
        cls=LaunchCommand,
        context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    )(launch)
