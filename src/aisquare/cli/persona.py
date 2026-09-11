"""Human-only persona controls; shared local parser also powers the TUI box."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Annotated

import typer

from aisquare.core.personas import MAX_PACK_BYTES, pack_bytes, plain_text
from aisquare.core.spawn import untraced_env
from aisquare.core.state import get_state
from aisquare.services import personas

app = typer.Typer(
    help=(
        "Manage display-only role personalities. Commands: list, status, preview PACK, "
        "use PACK [--role ROLE] [--project PROJECT|--global], off, reset, "
        "add FILE|--url URL|--name NAME --text TEXT, edit PACK, export PACK --output FILE, "
        "remove PACK, voice on|off. Type /persona in the AI Square command box, "
        "not the Claude terminal."
    ),
    invoke_without_command=True,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)


@app.callback()
def main(
    ctx: typer.Context,
    global_scope: Annotated[
        bool, typer.Option("--global", help="Show or change the global fallback.")
    ] = False,
    project: Annotated[
        str | None, typer.Option("--project", help="Registered project ID or name.")
    ] = None,
) -> None:
    """Show project status when no subcommand is given."""
    if ctx.invoked_subcommand is None:
        scope = ["--global"] if global_scope else (["--project", project] if project else [])
        _run(shlex.join(["status", *scope, *ctx.args]))


def _passthrough(ctx: typer.Context) -> list[str]:
    """A trailing ``--json`` belongs to us, like on every other command group."""
    words = list(ctx.args)
    if "--json" in words:
        words.remove("--json")
        get_state().json_output = True
    return words


def _run(command: str) -> None:
    try:
        receipt = personas.run_persona_command(command)
        if receipt.editor is not None:
            if not sys.stdin.isatty() or get_state().json_output:
                raise personas.PersonaUsageError(
                    "text authoring/editing needs an interactive terminal; "
                    "import a ready JSON pack instead"
                )
            typer.echo(receipt.message)
            edited = _edit_pack(pack_bytes(receipt.editor).decode())
            if edited is None:
                receipt = personas.PersonaReceipt(action="cancel", message="No persona saved.")
            else:
                receipt = personas.save_draft(edited)
        if get_state().json_output:
            typer.echo(receipt.model_dump_json(exclude_none=True))
        else:
            typer.echo(plain_text(receipt.message))
            if receipt.data:
                typer.echo(plain_text(json.dumps(receipt.data, indent=2, ensure_ascii=False)))
    except (OSError, ValueError, KeyError) as exc:
        # A malformed command line is a usage error (2); a real failure such as an
        # unknown pack, a bad file or a refused download is a runtime error (1).
        usage = isinstance(exc, personas.PersonaUsageError)
        if get_state().json_output:
            typer.echo(
                json.dumps({"error": "usage" if usage else "persona_error", "detail": str(exc)})
            )
        else:
            typer.echo(f"Persona: {plain_text(str(exc))}", err=True)
        raise typer.Exit(2 if usage else 1) from exc


def _edit_pack(initial: str) -> str | None:
    """Edit a private temporary JSON file; editor argv never goes through a shell."""
    command = (
        os.environ.get("VISUAL")
        or os.environ.get("EDITOR")
        or ("notepad" if os.name == "nt" else "vi")
    )
    words = shlex.split(command)
    if not words:
        raise ValueError("VISUAL/EDITOR must name an editor program")
    with tempfile.TemporaryDirectory(prefix="asq-persona-edit-") as temporary:
        path = Path(temporary) / "persona.json"
        path.write_text(initial, encoding="utf-8")
        original_time = path.stat().st_mtime_ns
        result = subprocess.run([*words, str(path)], check=False, env=untraced_env(os.environ))
        if result.returncode != 0:
            raise ValueError(f"editor exited with status {result.returncode}; no persona saved")
        if path.is_symlink() or not path.is_file():
            raise ValueError("editor must save a regular JSON file")
        if path.stat().st_mtime_ns == original_time:
            return None
        with path.open("rb") as stream:
            content = stream.read(MAX_PACK_BYTES + 1)
        if len(content) > MAX_PACK_BYTES:
            raise ValueError("edited persona exceeds the size limit")
        return content.decode("utf-8")


def _command(ctx: typer.Context) -> None:
    """Run a local persona action. See `asq persona --help` for supported forms."""
    _run(shlex.join([ctx.info_name or "status", *_passthrough(ctx)]))


def _pack_command(
    ctx: typer.Context,
    pack: Annotated[str, typer.Argument(help="Pack ID or ID@version.", metavar="PACK")],
) -> None:
    """Run a local persona action on one pack. See `asq persona --help`."""
    _run(shlex.join([ctx.info_name or "status", pack, *_passthrough(ctx)]))


def _export_command(
    ctx: typer.Context,
    pack: Annotated[str, typer.Argument(help="Pack ID or ID@version.", metavar="PACK")],
    output: Annotated[str, typer.Option("--output", help="Destination JSON file.")],
) -> None:
    """Export one pack as a JSON file; refuses to overwrite an existing file."""
    _run(shlex.join(["export", pack, "--output", output, *_passthrough(ctx)]))


def _voice_command(
    ctx: typer.Context,
    state: Annotated[str, typer.Argument(help="on or off.", metavar="on|off")],
) -> None:
    """Opt this project's NEW agents into the selected pack's speaking style.

    The one persona setting that reaches an agent: `asq launch` appends the
    pack's voice instruction to the system prompt. Records, evidence, rules and
    the panel are unaffected; running sessions do not change.
    """
    _run(shlex.join(["voice", state, *_passthrough(ctx)]))


_EXTRA = {"allow_extra_args": True, "ignore_unknown_options": True}
for _name in ("list", "status", "off", "reset", "add"):
    app.command(_name, context_settings=_EXTRA)(_command)
app.command("voice", context_settings=_EXTRA)(_voice_command)
for _name in ("preview", "use", "edit", "remove"):
    app.command(_name, context_settings=_EXTRA)(_pack_command)
app.command("export", context_settings=_EXTRA)(_export_command)
