"""``aisquare remote`` — the Remote Control server on one local port.

Each command imports ``services.remote_server`` in its own body. ``cli/app.py``
imports this module to register it, so an import here at module scope put the
server on every command's import path, every hook's included: about thirty more
modules, asyncio among them, and on Windows asyncio loads ``_overlapped``, which
opens a socket at import. A child started without ``SYSTEMROOT`` cannot (WinError
10106), so ``asq --json`` and every hook died before typer ran (PR #243's
windows leg). ``tests/test_remote_stays_off_the_hook_path.py`` keeps it off.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from aisquare.cli.common import fail
from aisquare.core.console import stderr_console, stdout_console
from aisquare.core.state import get_state

if TYPE_CHECKING:
    from aisquare.services.remote_server import RemoteInfo

#: ``remote_server.DEFAULT_PORT``, spelled out so ``--port`` needs no import (the
#: ``cli/serve.py`` shape); the hook-path test pins the two equal.
DEFAULT_PORT = 8748

app = typer.Typer(
    help="Remote Control: show the fleet to a phone over one local port (ngrok exposes it).",
    no_args_is_help=True,
)


def _fail_if_missing() -> None:
    from aisquare.services import remote_server

    problem = remote_server._remote_dependency_error()
    if problem is not None:
        fail(problem, error="remote_not_installed")


def _fail_if_no_page(dist: Path | None) -> None:
    from aisquare.services import remote_server

    problem = remote_server._page_missing(dist)
    if problem is not None:
        fail(problem, error="no_remote_page")


def _describe_remote(info: RemoteInfo, *, allow_write: bool) -> dict[str, object]:
    from aisquare.services import remote_server

    return {
        "url_local": info.url_local,
        "token": info.token,
        "password": info.password,
        "allow_write": allow_write,
        "bind": remote_server.BIND,
    }


@app.command("serve")
def serve_remote(
    port: Annotated[
        int, typer.Option("--port", help="Local port.", envvar="AISQUARE_REMOTE_PORT")
    ] = DEFAULT_PORT,
    dist: Annotated[
        Path | None,
        typer.Option(
            "--dist",
            help="Built aisquare-remote page to serve (default ~/.aisquare/remote-dist).",
        ),
    ] = None,
) -> None:
    """Serve the page, the read-only JSON API and the live stream on 127.0.0.1 (Ctrl-C stops)."""
    from aisquare.services import remote_server

    _fail_if_missing()
    _fail_if_no_page(dist)
    state = remote_server.runtime()
    info = state.connection_info(port)
    payload = _describe_remote(info, allow_write=state.allow_write)
    if get_state().json_output:
        typer.echo(json.dumps(payload), err=False)
    else:
        console = stderr_console()
        console.print(f"Remote Control on {info.url_local}", markup=False)
        console.print(f"password: {info.password}", markup=False)
        gate = "ON — writes are audited" if state.allow_write else "off (read-only)"
        console.print(f"write actions: {gate}   · toggle: aisquare remote allow-write on|off")
        console.print(f"expose with: ngrok http {port}   · Ctrl-C stops", markup=False)
    try:
        remote_server.run_foreground(dist, port)
    except OSError as exc:
        fail(f"cannot bind {remote_server.BIND}:{port} — {exc}", error="remote_bind_failed")


@app.command("install-page")
def install_page(
    dist: Annotated[
        Path,
        typer.Argument(help="Built aisquare-remote dist/ directory (must contain index.html)."),
    ],
) -> None:
    """Copy a built ``aisquare-remote`` page into ``~/.aisquare/remote-dist`` (atomic replace).

    ``R`` (the Remote modal) on a fresh machine has nowhere to serve from until this runs once —
    it is not something a fresh clone can do for itself (the built page lives
    in the FE repo's dist/, not in this package).
    """
    from aisquare.services import remote_server

    source = dist.resolve()
    if not (source / "index.html").is_file():
        fail(
            f"no index.html in {source} — build aisquare-remote first (npm run build)",
            error="invalid_dist",
            ref=str(source),
        )
    destination = remote_server.install_page(source)
    if get_state().json_output:
        typer.echo(json.dumps({"installed": str(destination)}))
    else:
        stdout_console().print(f"✓ installed the remote page → {destination}", markup=False)


@app.command("status")
def status_command() -> None:
    """The link, the password and the unlocked devices, from ~/.aisquare/remote.json."""
    from aisquare.services import remote_server

    state = remote_server.runtime()
    payload = _describe_remote(state.connection_info(), allow_write=state.allow_write)
    payload["sessions"] = state.device_rows()
    if get_state().json_output:
        typer.echo(json.dumps(payload))
        return
    console = stdout_console()
    console.print(f"url:         {payload['url_local']}", markup=False)
    console.print(f"password:    {payload['password']}", markup=False)
    console.print(f"allow_write: {'on' if state.allow_write else 'off'}", markup=False)
    devices = state.device_rows()
    console.print(f"devices:     {len(devices)}", markup=False)
    for device in devices:
        console.print(f"  {device['sid']}  {device['ua'][:40]}  last {device['last_seen']}")


@app.command("allow-write")
def allow_write(
    switch: Annotated[str, typer.Argument(help="on or off (default off; never on by itself).")],
) -> None:
    """Turn the write endpoints on or off for the running/next server."""
    if switch not in ("on", "off"):
        fail(f"say 'on' or 'off', not {switch!r}", error="invalid_switch", ref=switch)
    from aisquare.services import remote_server

    remote_server.set_allow_write(switch == "on")
    if get_state().json_output:
        typer.echo(json.dumps({"allow_write": switch == "on"}))
    else:
        stdout_console().print(f"✓ write actions {switch}", markup=False)


@app.command("regenerate-password")
def regenerate_password() -> None:
    """Mint a new password; every unlocked device has to unlock again."""
    from aisquare.services import remote_server

    password = remote_server.regenerate_password()
    if get_state().json_output:
        typer.echo(json.dumps({"password": password}))
    else:
        stdout_console().print(f"✓ new password: {password}", markup=False)


@app.command("revoke")
def revoke_command(
    sid: Annotated[str, typer.Argument(help="Device session id (see status).")],
) -> None:
    """Drop one unlocked device."""
    from aisquare.services import remote_server

    if not remote_server.revoke_remote_device(sid):
        fail(f"no device {sid}", error="not_found", ref=sid)
    if get_state().json_output:
        typer.echo(json.dumps({"revoked": sid}))
    else:
        stdout_console().print(f"✓ revoked {sid}", markup=False)
