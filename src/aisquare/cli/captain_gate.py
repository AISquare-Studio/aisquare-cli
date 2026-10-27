"""``aisquare captain gate``: the owner gate (#216) from the terminal.

- ``enroll [--replace]``: write the secret and show the QR (and the URI) an authenticator
  app scans. The captain has no tool for this: only the owner's terminal enrols.
- ``unlock <code>`` / ``lock``: the captain's ``unlock`` and ``lock`` tools, through the same
  audited frame as every other verb (``cli/captain_verbs.py``), the code masked in the audit.
- ``status``: whether the gate applies, is ready, and which window is open.

Every service import is inside its command: importing the CLI loads no captain module.
"""

from __future__ import annotations

import json
from typing import Annotated

import typer

from aisquare.cli.captain_verbs import _emit_json, _perform
from aisquare.cli.common import fail
from aisquare.core.console import stdout_console
from aisquare.core.state import get_state

app = typer.Typer(
    help="The owner gate: a code from your authenticator lets the captain act as you for "
    "15 minutes.",
    no_args_is_help=True,
)


def register(parent: typer.Typer) -> None:
    """Put ``gate`` on the ``captain`` group (``cli.captain``)."""
    parent.add_typer(app, name="gate")


@app.command("enroll")
def enroll(
    replace: Annotated[
        bool,
        typer.Option("--replace", help="Make a new secret; the old one stops working."),
    ] = False,
) -> None:
    """Make the gate's secret and show the QR to scan with an authenticator app."""
    from aisquare.services.captain import gate, voice
    from aisquare.services.captain.errors import Refused

    try:
        enrolled = gate.enroll(replace=replace)
    except Refused as exc:
        fail(str(exc), error="refused", detail=str(exc))
    if get_state().json_output:
        typer.echo(json.dumps({"uri": enrolled.uri, "path": str(enrolled.path)}))
        return
    console = stdout_console()
    console.print(f"✓ enrolled: the secret is in {enrolled.path} (mode 0600)", markup=False)
    qr = voice.qr_lines(enrolled.uri)
    if qr is not None:
        console.print("scan this with an authenticator app:", markup=False)
        for line in qr:
            console.print(line, markup=False, highlight=False)
    console.print(f"or add it by hand: {enrolled.uri}", markup=False, highlight=False)
    console.print(
        "then unlock with a code: say it to the captain, or `aisquare captain gate unlock <code>`",
        markup=False,
    )


@app.command("unlock")
def unlock(
    code: Annotated[str, typer.Argument(help="The six-digit code your authenticator shows.")],
) -> None:
    """Let the captain act as you for 15 minutes."""
    result = _perform("unlock", {"code": code})
    if get_state().json_output:
        _emit_json(result)
        return
    stdout_console().print(
        f"✓ unlocked until {result.get('until')} · window {result.get('window')} · action seq "
        f"{result.get('action_seq')}",
        markup=False,
    )


@app.command("lock")
def lock() -> None:
    """Close the window now: the captain stops acting as you until the next code."""
    result = _perform("lock", {})
    if get_state().json_output:
        _emit_json(result)
        return
    closed = "locked" if result.get("was_open") else "locked (no window was open)"
    stdout_console().print(f"✓ {closed} · action seq {result.get('action_seq')}", markup=False)


@app.command("status")
def status() -> None:
    """Whether the gate applies, whether it can verify a code, and the open window."""
    from aisquare.services.captain import gate

    found = gate.status()
    if get_state().json_output:
        typer.echo(json.dumps(found))
        return
    console = stdout_console()
    if not found["active"]:
        why = "the captain is off" if not found["captain"] else "[captain] gate = false"
        console.print(f"the owner gate is off ({why}): every tool runs as in Phase 1")
        return
    if found["why_not"]:
        console.print(f"the owner gate is on and locked: {found['why_not']}", markup=False)
    elif found["window"]:
        console.print(f"open until {found['until']} (window {found['window']})", markup=False)
    else:
        console.print(f"locked: {gate.UNLOCK}", markup=False)
    if found["failures"]:
        console.print(f"{found['failures']} wrong code(s) in a row", style="dim")
    if found["locked_until"]:
        console.print(f"code entry locked until {found['locked_until']}", style="dim")
