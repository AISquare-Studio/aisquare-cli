"""Hidden hook handlers invoked by Claude Code (installed via ``agents connect``).

These run on every session start / prompt submit, so they are defensive: any
failure is swallowed and the command exits 0, never disrupting the agent.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import typer

from aisquare.core import paths
from aisquare.services import hooks as hooks_service
from aisquare.services.team import ManagerWakeupError

app = typer.Typer(
    help="Internal hook handlers for agent integrations.",
    hidden=True,
    no_args_is_help=True,
)


#: What each boundary actually costs when it fails open. Named per hook rather
#: than shared, because three of the five do not inject context at all and a
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
    "manager-wakeup": "the manager will not be woken by this turn's board updates",
}


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


def _cwd(payload: dict[str, Any]) -> Path | None:
    cwd = payload.get("cwd")
    return Path(cwd) if isinstance(cwd, str) and cwd else None


def _str(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _flag(payload: dict[str, Any], key: str) -> bool:
    """A boolean payload field, read the way a LOOP GUARD must be read.

    Absent, ``null`` and ``false`` are off — an older payload without the field
    must still let the manager be woken, or the feature dies in silence. Any
    other value is on, a stray string ``"false"`` included: misreading "already
    continuing" as "not" is the mistake that opens a loop, and the cost of the
    other mistake is one wake-up deferred to the next prompt.
    """
    return bool(payload.get(key))


def _effort_level(payload: dict[str, Any]) -> str | None:
    """The ``effort.level`` field of a hook payload (an object; optional)."""
    effort = payload.get("effort")
    if not isinstance(effort, dict):
        return None
    level = effort.get("level")
    return level if isinstance(level, str) and level else None


@app.command("session-start")
def session_start() -> None:
    """Emit aisquare context for a starting session (stdout becomes context)."""
    try:
        payload = _payload()
        context = hooks_service.session_start_context(
            _cwd(payload),
            session_id=_str(payload, "session_id"),
            source=_str(payload, "source"),
            transcript_path=_str(payload, "transcript_path"),
            model=_str(payload, "model"),
            effort=_effort_level(payload),
        )
    except Exception as exc:  # never disrupt the agent
        _cost_of_failing_open("session-start", exc)
        return
    if context:
        typer.echo(context)


@app.command("user-prompt-submit")
def user_prompt_submit() -> None:
    """Capture the prompt and emit teammate updates (stdout becomes context)."""
    try:
        payload = _payload()
        prompt = payload.get("prompt")
        delta = hooks_service.prompt_submitted(
            prompt if isinstance(prompt, str) else None,
            _cwd(payload),
            session_id=_str(payload, "session_id"),
            transcript_path=_str(payload, "transcript_path"),
            model=_str(payload, "model"),
            effort=_effort_level(payload),
        )
    except Exception as exc:  # never disrupt the agent
        _cost_of_failing_open("user-prompt-submit", exc)
        return
    if delta:
        typer.echo(delta)


@app.command("session-end")
def session_end() -> None:
    """Mark the session ended on the orchestrator (no output)."""
    try:
        payload = _payload()
        hooks_service.session_ended(_cwd(payload), session_id=_str(payload, "session_id"))
    except Exception as exc:  # never disrupt the agent
        _cost_of_failing_open("session-end", exc)
        return


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
    try:
        payload = _payload()
        decision = hooks_service.turn_stopped(
            _cwd(payload),
            session_id=_str(payload, "session_id"),
            stop_hook_active=_flag(payload, "stop_hook_active"),
        )
    except ManagerWakeupError as exc:  # the row is right; only the wake-up was lost
        _cost_of_failing_open("stop", exc.cause, cost=_COST["manager-wakeup"])
        return
    except Exception as exc:  # never disrupt the agent
        _cost_of_failing_open("stop", exc)
        return
    if decision is not None:
        typer.echo(json.dumps(decision.as_hook_output()))


@app.command("notification")
def notification() -> None:
    """Mark the session as needing attention (no output)."""
    try:
        payload = _payload()
        hooks_service.needs_attention(
            _cwd(payload),
            session_id=_str(payload, "session_id"),
            message=_str(payload, "message"),
        )
    except Exception as exc:  # never disrupt the agent
        _cost_of_failing_open("notification", exc)
        return
