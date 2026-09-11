"""``aisquare remote`` — the Remote Control server on one local port."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from aisquare.cli.common import fail
from aisquare.core.console import stderr_console, stdout_console
from aisquare.core.state import get_state
from aisquare.services import remote_server

app = typer.Typer(
    help="Remote Control: show the fleet to a phone over one local port (ngrok exposes it).",
    no_args_is_help=True,
)


def _fail_if_missing() -> None:
    problem = remote_server._dependency_error()
    if problem is not None:
        fail(problem, error="remote_not_installed")


def _describe(info: remote_server.RemoteInfo, *, allow_write: bool) -> dict[str, object]:
    return {
        "url_local": info.url_local,
        "token": info.token,
        "password": info.password,
        "allow_write": allow_write,
        "bind": remote_server.BIND,
    }


@app.command("serve")
def serve(
    port: Annotated[
        int, typer.Option("--port", help="Local port.", envvar="AISQUARE_REMOTE_PORT")
    ] = remote_server.DEFAULT_PORT,
    dist: Annotated[
        Path | None,
        typer.Option(
            "--dist",
            help="Built aisquare-remote page to serve (default ~/.aisquare/remote-dist).",
        ),
    ] = None,
) -> None:
    """Serve the page, the read-only JSON API and the live stream on 127.0.0.1 (Ctrl-C stops)."""
    _fail_if_missing()
    state = remote_server.runtime()
    info = state.info(port)
    payload = _describe(info, allow_write=state.allow_write)
    if get_state().json_output:
        typer.echo(json.dumps(payload), err=False)
    else:
        console = stderr_console()
        console.print(f"Remote Control on {info.url_local}", markup=False)
        console.print(f"password: {info.password}", markup=False)
        gate = "ON — writes are audited" if state.allow_write else "off (read-only)"
        console.print(f"write actions: {gate}   · toggle: aisquare remote allow-write on|off")
        console.print("expose with: ngrok http 8748   · Ctrl-C stops", markup=False)
    try:
        remote_server.run_foreground(dist, port)
    except OSError as exc:
        fail(f"cannot bind {remote_server.BIND}:{port} — {exc}", error="remote_bind_failed")


@app.command("status")
def status() -> None:
    """The link, the password and the unlocked devices, from ~/.aisquare/remote.json."""
    state = remote_server.runtime()
    payload = _describe(state.info(), allow_write=state.allow_write)
    payload["sessions"] = state.devices()
    if get_state().json_output:
        typer.echo(json.dumps(payload))
        return
    console = stdout_console()
    console.print(f"url:         {payload['url_local']}", markup=False)
    console.print(f"password:    {payload['password']}", markup=False)
    console.print(f"allow_write: {'on' if state.allow_write else 'off'}", markup=False)
    devices = state.devices()
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
    remote_server.set_allow_write(switch == "on")
    if get_state().json_output:
        typer.echo(json.dumps({"allow_write": switch == "on"}))
    else:
        stdout_console().print(f"✓ write actions {switch}", markup=False)


@app.command("regenerate-password")
def regenerate_password() -> None:
    """Mint a new password; every unlocked device has to unlock again."""
    password = remote_server.regenerate_password()
    if get_state().json_output:
        typer.echo(json.dumps({"password": password}))
    else:
        stdout_console().print(f"✓ new password: {password}", markup=False)


@app.command("revoke")
def revoke(sid: Annotated[str, typer.Argument(help="Device session id (see status).")]) -> None:
    """Drop one unlocked device."""
    if not remote_server.revoke(sid):
        fail(f"no device {sid}", error="not_found", ref=sid)
    if get_state().json_output:
        typer.echo(json.dumps({"revoked": sid}))
    else:
        stdout_console().print(f"✓ revoked {sid}", markup=False)
