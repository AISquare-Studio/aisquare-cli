"""Hidden hook handlers invoked by the coding agents (installed via ``agents connect``).

Every one decodes its payload with the agent's adapter
(:meth:`~aisquare.core.agent_adapters.types.AgentAdapter.decode_event`), runs the shared
services (``services.agent_events.handle``) and prints the adapter's encoding of the
reply. Claude Code's installed commands name their event (``aisquare hook
session-start``…), Codex's is ``aisquare hook codex``, and any other agent's is ``aisquare
hook event --agent <id>``, so a new agent needs no subcommand of its own.

These run on every session start / prompt submit, so they are defensive: any
failure is swallowed and the command exits 0, never disrupting the agent.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from aisquare.core import paths
from aisquare.core.agent_adapters.types import GENERIC_HOOK, AgentAdapter
from aisquare.services import hooks as hooks_service
from aisquare.services.team import ManagerWakeupError, TurnEndDeliveryError

app = typer.Typer(
    help="Internal hook handlers for agent integrations.",
    hidden=True,
    no_args_is_help=True,
)


#: What each boundary actually costs when it fails open. Named per hook rather
#: than shared, because four of the six do not inject context at all and a
#: line claiming they did would be a wrong sentence printed on every turn — the
#: two that matter to a reader are the ones a TEAMMATE sees: a session that
#: never leaves "running", and a turn that arrives with no delta.
#:
#: ``manager-wakeup`` is not a hook but the one branch of ``stop`` that fails
#: open on its own (docs/plans/fleet-tui.md §5.5): when it does, the row IS
#: marked waiting, so the ``stop`` line would be the wrong sentence.
_COST = {
    "session-start": "this session starts with no aisquare context",
    "user-prompt-submit": "no teammate updates reach this turn",
    "session-end": "the board will keep showing this session as running",
    "stop": "the board will not show this session as waiting for input",
    "notification": "the board will not show this session as needing attention",
    "stop-failure": "the board will not show this session as limited, and no hand-over will run",
    "manager-wakeup": "the manager will not be woken by this turn's board updates",
    "turn-end-delivery": "the notes addressed to it wait for its next prompt",
}
#: What an agent's one entry for every event (``hook codex``, ``hook event``) costs.
_NATIVE_COST = "this native event was not recorded"


def _cost_of_failing_open(hook: str, exc: Exception, *, cost: str | None = None) -> None:
    """Never disrupt the agent — and never lose the reason either.

    Failing open is half the doctrine. The other half is saying what it cost,
    and these five boundaries were doing only the first: a damaged store made
    every session start with no context and every prompt arrive with no
    teammate delta, silently, with ``doctor`` the only surface that knew and
    nothing to suggest running it.

    STDERR, NEVER STDOUT. For ``session-start`` and ``user-prompt-submit``
    stdout BECOMES the agent's context, so a diagnostic printed there is
    injected into the model's prompt — a worse defect than the silence it
    would replace. ``typer.echo(..., err=True)`` matches ``emit_write_warning``
    in the team CLI, which is already the agent-facing warning channel.

    EVERY OCCURRENCE, not once per session: warning once would need somewhere
    to record that it already warned, and the thing that is broken IS the place
    we would record it. Each occurrence is a real loss — that prompt genuinely
    did not get its delta — and the volume is bounded by fixing the store,
    which is what the line says to do.
    """
    if isinstance(exc, sqlite3.Error):
        reason = f"{paths.db_path()} unreadable ({exc})"
    else:
        reason = f"{hook} failed ({type(exc).__name__}: {exc})"
    cost = cost or _COST.get(hook, "this hook did nothing")
    typer.echo(f"aisquare: {reason} — {cost}; run: aisquare doctor", err=True)


def _payload() -> dict[str, Any]:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _run(
    hook: str,
    adapter: AgentAdapter,
    payload: dict[str, Any],
    *,
    config_dir: str | None = None,
    definition: str | None = None,
    cost: str | None = None,
) -> None:
    """Handle one hook call of ``adapter``'s and print its reply; never raise.

    stdout is what the agent reads (context, or a Stop decision as JSON), so it carries
    the reply alone; every failure goes to stderr (:func:`_cost_of_failing_open`), with
    ``cost`` saying what it cost. Printed inside the ``try``: the write itself can fail on
    what the context holds (a character stdout cannot encode), and that must not become
    a traceback and a non-zero exit either.
    """
    from aisquare.core import agents
    from aisquare.services import agent_events

    try:
        home = adapter.config_dir(
            agents._home(), os.environ, Path(config_dir) if config_dir else None
        )
        output = agent_events.handle(adapter, payload, home, definition=definition)
        if output:
            typer.echo(output)
    except ManagerWakeupError as exc:  # the row is right; only the wake-up was lost
        _cost_of_failing_open(hook, exc.cause, cost=_COST["manager-wakeup"])
    except TurnEndDeliveryError as exc:  # the row is right; only the hand-over was lost
        _cost_of_failing_open(hook, exc.cause, cost=_COST["turn-end-delivery"])
    except Exception as exc:  # never disrupt the agent
        _cost_of_failing_open(hook, exc, cost=cost)


def _installed(hook: str) -> None:
    """A subcommand that names its event (Claude Code's ``session-start``…): the payload
    is that event's, whatever it says, for the adapter that installed the subcommand."""
    from aisquare.core.agent_adapters import adapters

    try:
        adapter, spec = next(
            (adapter, spec)
            for adapter in adapters()
            for spec in adapter.capabilities.hooks
            if spec.command == hook
        )
        payload = {**_payload(), "hook_event_name": spec.event}
    except Exception as exc:  # never disrupt the agent
        _cost_of_failing_open(hook, exc)
        return
    _run(hook, adapter, payload)


@app.command("session-start")
def session_start() -> None:
    """Emit aisquare context for a starting session (stdout becomes context)."""
    _installed("session-start")


@app.command("user-prompt-submit")
def user_prompt_submit() -> None:
    """Capture the prompt and emit teammate updates (stdout becomes context)."""
    _installed("user-prompt-submit")


@app.command("session-end")
def session_end() -> None:
    """Mark the session ended on the orchestrator (no output)."""
    _installed("session-end")


@app.command("stop")
def stop() -> None:
    """Mark the session as waiting for input (turn finished).

    stdout is JSON or nothing. It is nothing for every role but ``manager``, and
    for a manager it is the Stop decision that keeps the turn going when
    teammates put decisions on the board (docs/plans/fleet-tui.md §7.3):
    ``{"decision": "block", "reason": "<the delta>"}``. ``stop_hook_active`` is
    Claude Code saying it is already continuing on a stop hook; it is honoured
    as the loop guard the reference asks for.
    """
    _installed("stop")


@app.command("stop-failure")
def stop_failure() -> None:
    """The turn ended on an API error instead of a Stop (no output).

    Claude Code fires ``StopFailure`` with ``error`` (``rate_limit``,
    ``overloaded``, ``authentication_failed``…), an optional ``error_details``
    and ``last_assistant_message`` — for a usage limit, the rendered
    ``You've hit your session limit · resets 12:30am (…)`` line. A
    ``rate_limit`` parks the session as ``limited`` on the board with that reset
    time and wakes the manager; any other error puts it back to ``waiting`` with
    a feed line naming the error (#146). Output is ignored by Claude Code, so
    nothing is printed on purpose.
    """
    _installed("stop-failure")


@app.command("hand-over", hidden=True)
def hand_over(
    session_id: Annotated[str, typer.Argument(help="The limited session's id.")],
    reason: Annotated[
        str | None, typer.Option("--reason", help="Why (recorded on the board).")
    ] = None,
) -> None:
    """The detached half of an automatic hand-over (no output; started by ``stop-failure``).

    Not a Claude Code hook: ``stop-failure`` starts this in a worker of its own
    session, because the hook itself is a child of the pane ``fleet switch``
    kills (#146; review of #205, finding 1). Silent like every hook; the
    board carries a refusal (``not switched — …``), and what a switch that went
    ahead could not do (``switched — …``).
    """
    try:
        hooks_service.hand_over(session_id, reason=reason)
    except Exception as exc:  # the board carries the refusal; nothing else to disrupt
        _cost_of_failing_open("hand-over", exc, cost="the limited agent was not moved")
        return


@app.command("notification")
def notification() -> None:
    """A Claude Code notification (no output): a real prompt rings the bell, an idle notice not.

    Claude Code sends ``Notification`` for several unrelated reasons and names
    them in ``notification_type`` — ``permission_prompt``, ``idle_prompt``,
    ``auth_success``, ``elicitation_dialog``, the ``quota_auto_resume_*``
    family… (#153). Only the ones that need a human become ``attention``; the
    routine idle notice leaves the session as it was; the rest are feed lines.
    """
    _installed("notification")


@app.command("codex")
def codex(
    config_dir: str | None = typer.Option(None, "--config-dir"),
    definition: str | None = typer.Option(None, "--definition"),
) -> None:
    """Codex's installed hook command: ``hook event --agent codex`` by its 0.8.0 name."""
    from aisquare.core.agent_adapters import get_adapter

    _run(
        "codex",
        get_adapter("codex"),
        _payload(),
        config_dir=config_dir,
        definition=definition,
        cost=_NATIVE_COST,
    )


@app.command(GENERIC_HOOK)
def event(
    agent: str = typer.Option(..., "--agent", help="The adapter that installed this hook."),
    hook_event: str | None = typer.Option(
        None, "--event", help="The native event, where the payload does not name it."
    ),
    config_dir: str | None = typer.Option(None, "--config-dir"),
    definition: str | None = typer.Option(None, "--definition"),
) -> None:
    """Any agent's native hook: decoded by its adapter, run through the shared services.

    The one entry an adapter needs (``agent_adapters.types.hook_args`` writes it): the
    event is ``--event`` or the payload's ``hook_event_name``.
    """
    from aisquare.core.agent_adapters import get_adapter

    try:
        adapter = get_adapter(agent)
        payload = _payload()
        if hook_event:
            payload = {**payload, "hook_event_name": hook_event}
    except Exception as exc:  # never disrupt the agent
        _cost_of_failing_open(GENERIC_HOOK, exc, cost=_NATIVE_COST)
        return
    _run(
        GENERIC_HOOK,
        adapter,
        payload,
        config_dir=config_dir,
        definition=definition,
        cost=_NATIVE_COST,
    )
