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


@app.command("session-start")
def session_start() -> None:
    """Emit aisquare context for a starting session (stdout becomes context)."""
    try:
        context = hooks_service.session_start_context(_cwd(_payload()))
    except Exception:  # never disrupt the agent
        return
    if context:
        typer.echo(context)


@app.command("user-prompt-submit")
def user_prompt_submit() -> None:
    """Capture the submitted user prompt (no output)."""
    try:
        payload = _payload()
        prompt = payload.get("prompt")
        if isinstance(prompt, str):
            hooks_service.capture_prompt(prompt, _cwd(payload))
    except Exception:  # never disrupt the agent
        return
