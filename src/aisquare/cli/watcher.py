"""``aisquare watch``: the fleet's one non-AI watcher (P2-W2, #284).

``cli/watch.py`` is the board's ``--watch``; this is the watcher. The work is
``services.watch``; this module parses, prints and honours ``--json``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated

import typer

from aisquare.cli.common import fail
from aisquare.core.console import stderr_console, stdout_console
from aisquare.core.state import get_state
from aisquare.services import watch as watch_service

app = typer.Typer(
    help="Watch the fleet without a model: dialogs, limits, stalls and pull requests wake "
    "a manager only when something is new.",
    invoke_without_command=True,
)


def _print_tick(report: watch_service.TickReport) -> None:
    if get_state().json_output:
        typer.echo(json.dumps(report.as_json()))
        return
    console = stdout_console()
    stamp = report.at.strftime("%H:%M:%S")
    if not report.new:
        console.print(f"{stamp} nothing new ({len(report.items)} open)", markup=False)
    for item in report.new:
        console.print(f"{stamp} {item.project_name} {item.kind}: {item.summary}", markup=False)
    for path in report.files.values():
        console.print(f"  → {path}", markup=False)
    for label in report.stopped:
        console.print(f"  auto-stopped {label}", markup=False)
    for note in report.notes:
        stderr_console().print(f"aisquare watch: {note}", markup=False)


@app.callback()
def watch(
    ctx: typer.Context,
    interval: Annotated[
        float | None,
        typer.Option(
            "--interval", help="Seconds between looks (default: \\[watch] interval_seconds)."
        ),
    ] = None,
    once: Annotated[bool, typer.Option("--once", help="Look once, deliver, and exit.")] = False,
    detach: Annotated[
        bool,
        typer.Option("--detach", help="Run in a window of the fleet's tmux server, and return."),
    ] = False,
    stop: Annotated[bool, typer.Option("--stop", help="Stop the running watcher.")] = False,
    status: Annotated[
        bool, typer.Option("--status", help="Whether a watcher runs, and its last look.")
    ] = False,
) -> None:
    """Look every interval; for each project with something new, write the details to a
    file (mode 600), put one ``watch`` line on its board for the manager with the counts
    and the path, and nudge the manager. One watcher per home."""
    if ctx.invoked_subcommand is not None:
        return
    if sum([once, detach, stop, status]) > 1:
        fail("--once, --detach, --stop and --status are one each", error="usage", exit_code=2)
    if status:
        _print_watch_status()
        return
    if stop:
        pid = watch_service.stop_detached()
        _say({"stopped": pid}, f"stopped the watcher (pid {pid})" if pid else "no watcher runs")
        return
    if detach:
        try:
            pid = watch_service.detach()
        except watch_service.AlreadyWatching as exc:
            fail(str(exc), error="already_watching")
        _say(
            {"pid": pid, "session": watch_service.DETACHED_SESSION},
            f"✓ watching (pid {pid}) in tmux session {watch_service.DETACHED_SESSION}; "
            "`aisquare watch --stop` ends it",
        )
        return
    if once:
        try:
            looked = watch_service.Watcher().tick()
        except TimeoutError:  # a running watcher's tick holds the state lock
            fail("another look is running; try again in a moment", error="busy")
        _print_tick(looked)
        return
    try:
        watch_service.run(
            interval=interval,
            on_tick=_print_tick,
            on_error=lambda said: stderr_console().print(f"aisquare watch: {said}", markup=False),
        )
    except watch_service.AlreadyWatching as exc:
        fail(str(exc), error="already_watching")


def _say(payload: Mapping[str, object], line: str) -> None:
    if get_state().json_output:
        typer.echo(json.dumps(payload))
    else:
        stdout_console().print(line, markup=False)


def _print_watch_status() -> None:
    found = watch_service.watch_status()
    age = found.age()
    payload = {
        "running": found.pid is not None,
        "pid": found.pid,
        "last_scan_at": found.last_scan_at.isoformat() if found.last_scan_at else None,
        "last_scan_age_seconds": int(age.total_seconds()) if age is not None else None,
    }
    if found.pid is None:
        line = "no watcher runs: `aisquare watch --detach` starts one"
    else:
        line = f"watching (pid {found.pid})"
    if age is not None:
        line += f"; last look {int(age.total_seconds())} s ago"
    _say(payload, line)


@app.command("show")
def show(
    path: Annotated[Path | None, typer.Argument(help="A detail file a watch line named.")] = None,
    last: Annotated[bool, typer.Option("--last", help="The newest detail file.")] = False,
) -> None:
    """Print the items a ``watch`` board line counted, from the file it names."""
    if path is not None and last:
        fail("give a path or --last, not both", error="usage", exit_code=2)
    try:
        data = watch_service.show(path)
    except (FileNotFoundError, ValueError) as exc:
        fail(str(exc), error="no_such_file")
    if get_state().json_output:
        typer.echo(json.dumps(data))
        return
    console = stdout_console()
    console.print(f"{data['path']} · {data.get('at')}", markup=False)
    for item in data.get("items") or []:
        console.print(f"- {item.get('kind')}: {item.get('summary')}", markup=False)
        detail = item.get("detail") or {}
        excerpt = detail.get("excerpt") if isinstance(detail, dict) else None
        url = detail.get("url") if isinstance(detail, dict) else None
        for extra in (excerpt, url):
            if extra:
                console.print(f"    {extra}", markup=False)
