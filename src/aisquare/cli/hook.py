"""Hidden hook handlers invoked by Claude Code (installed via ``agents connect``).

These run on every session start / prompt submit, so they are defensive: any
failure is swallowed and the command exits 0, never disrupting the agent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer

from aisquare.services import hooks as hooks_service

app = typer.Typer(
    help="Internal hook handlers for agent integrations.",
    hidden=True,
    no_args_is_help=True,
)


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


@app.command("session-start")
def session_start() -> None:
    """Emit aisquare context for a starting session (stdout becomes context)."""
    try:
        payload = _payload()
        context = hooks_service.session_start_context(
            _cwd(payload),
            session_id=_str(payload, "session_id"),
            source=_str(payload, "source"),
        )
    except Exception:  # never disrupt the agent
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
        )
    except Exception:  # never disrupt the agent
        return
    if delta:
        typer.echo(delta)


@app.command("session-end")
def session_end() -> None:
    """Mark the session ended on the team bus (no output)."""
    try:
        payload = _payload()
        hooks_service.session_ended(_cwd(payload), session_id=_str(payload, "session_id"))
    except Exception:  # never disrupt the agent
        return
