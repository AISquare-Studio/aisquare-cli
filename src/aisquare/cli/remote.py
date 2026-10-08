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
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from aisquare.cli.common import fail
from aisquare.core.console import stderr_console, stdout_console
from aisquare.core.state import get_state

if TYPE_CHECKING:
    from aisquare.services.remote_server import RemoteInfo, Runtime

#: ``remote_server.DEFAULT_PORT``, spelled out so ``--port`` needs no import (the
#: ``cli/serve.py`` shape); the hook-path test pins the two equal.
DEFAULT_PORT = 8750
#: ``serve --auto-off``: an hour, like the TUI's. A server nobody turns off is a link
#: anyone holding it can keep reaching; ``0`` (never) is a choice the banner names.
DEFAULT_AUTO_OFF_MINUTES = 60

#: The port in the link ``status`` and ``regenerate-password --new-link`` print: serve's,
#: from the same option and variable. Built for the default port, the link of a serve on
#: ``--port 18750`` or an exported ``AISQUARE_REMOTE_PORT`` refused every connection.
LinkPort = Annotated[
    int,
    typer.Option(
        "--port",
        help="The port serve runs on, for the link (serve's --port).",
        envvar="AISQUARE_REMOTE_PORT",
    ),
]

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


def _remote_runtime() -> Runtime:
    """The server's state from ``remote.json``, or a clean failure when it cannot be used.

    Every command that reads or writes the file starts here, so an unreadable or
    corrupt one is the same answer everywhere, its reason in ``--json``'s
    ``detail`` too (``fail`` keeps the message for the human surface alone).
    """
    from aisquare.services import remote_server

    try:
        return remote_server.runtime()
    except remote_server.RemoteError as exc:
        fail(str(exc), error="remote_state_unreadable", detail=str(exc))


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
            help="Serve this built page instead of the installed or bundled one.",
        ),
    ] = None,
    auto_off: Annotated[
        int,
        typer.Option(
            "--auto-off",
            min=0,
            metavar="MINUTES",
            envvar="AISQUARE_REMOTE_AUTO_OFF",
            help="Turn Remote off after this many minutes; a phone can extend it while writes "
            "are on. 0: never.",
        ),
    ] = DEFAULT_AUTO_OFF_MINUTES,
    public_url: Annotated[
        str | None,
        typer.Option(
            "--public-url",
            envvar="AISQUARE_REMOTE_NGROK_URL",
            help="The https URL phones reach this server at (ngrok's), for links in pushes.",
        ),
    ] = None,
) -> None:
    """Serve the page, the JSON API and the live stream on 127.0.0.1 until Ctrl-C or auto-off."""
    from aisquare.services import remote_server

    _fail_if_missing()
    _fail_if_no_page(dist)
    if public_url is not None and "://" not in public_url:
        public_url = f"https://{public_url}"  # ngrok's --url takes a bare host; so may this
    if public_url is not None:
        try:
            remote_server.check_public_origin(public_url)
        except ValueError as exc:
            fail(str(exc), error="invalid_public_url", ref=public_url)
    state = _remote_runtime()

    def banner() -> None:
        """Printed once the port is bound: a link for a server that never came up is a lie.

        Extending auto-off is a write, so the auto-off line offers it only with writes
        on: it said a phone could extend it under "write actions: off", where the page's
        Extend is greyed out (review of #243, round 3, 12/13). The switch is read once,
        for both lines.
        """
        info = state.connection_info(port)
        writes = state.allow_write
        payload = _describe_remote(info, allow_write=writes)
        deadline = state.auto_off_deadline()
        payload["auto_off_at"] = None if deadline is None else deadline.isoformat()
        if get_state().json_output:
            typer.echo(json.dumps(payload), err=False)
            return
        console = stderr_console()
        console.print(f"Remote Control on {info.url_local}", markup=False)
        console.print(f"password: {info.password}", markup=False)
        gate = "ON — writes are audited" if writes else "off (read-only)"
        console.print(
            f"write actions: {gate}   · toggle: aisquare remote allow-write on|off", markup=False
        )
        if deadline is None:
            console.print("auto-off: never (--auto-off 0)", markup=False)
        else:
            local = deadline.astimezone()
            extend = (
                "a phone can extend it" if writes else "no phone can extend it while writes are off"
            )
            console.print(
                f"auto-off: at {local:%H:%M} (in {auto_off} min) · {extend}", markup=False
            )
        if public_url is not None:
            origin = remote_server.check_public_origin(public_url)
            console.print(f"public link: {origin}/r/{info.token}/", markup=False)
        else:  # never learned from ngrok's local API, which anyone here can answer first
            console.print(
                "notifications open the page, not their card: --public-url <the ngrok URL> "
                "fixes that",
                markup=False,
            )
        # The inspector off: it keeps every request (the passphrase, the cookies) on a local
        # web interface that any user of this machine can read (ngrok_tunnel says more).
        console.print(
            f"expose with: ngrok http {port} --inspect=false   · Ctrl-C stops", markup=False
        )

    try:
        timed_out = remote_server.run_foreground(dist, port, auto_off, public_url, ready=banner)
    except remote_server.RemoteBindError as exc:
        fail(str(exc), error="remote_bind_failed", detail=str(exc))
    except remote_server.RemoteError as exc:
        fail(str(exc), error="remote_failed", detail=str(exc))
    except OSError as exc:  # remote.json would not write, say: anything but the port
        fail(f"the remote server could not run — {exc}", error="remote_failed", detail=str(exc))
    if timed_out:
        stderr_console().print("Remote turned off — the auto-off timer ran out", markup=False)


@app.command("install-page")
def install_page(
    dist: Annotated[
        Path,
        typer.Argument(help="Built aisquare-remote dist/ directory (must contain index.html)."),
    ],
) -> None:
    """Copy a built ``aisquare-remote`` page into ``~/.aisquare/remote-dist`` (atomic replace).

    A page installed here overrides the one bundled with aisquare-cli, for the
    Remote modal (``R``) and ``asq remote serve`` alike.
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
def status_command(port: LinkPort = DEFAULT_PORT) -> None:
    """The link, the password, the devices and failed unlocks, from ~/.aisquare/remote.json."""
    from aisquare.services import remote_server

    state = _remote_runtime()
    payload = _describe_remote(state.connection_info(port), allow_write=state.allow_write)
    status = remote_server.remote_server_status()
    rows = status["devices"]
    devices = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    payload["devices"] = devices
    payload["failed_unlocks"] = status["failed_unlocks"]
    payload["locked_out_until"] = status["locked_out_until"]
    if get_state().json_output:
        typer.echo(json.dumps(payload))
        return
    console = stdout_console()
    console.print(f"url:         {payload['url_local']}", markup=False)
    console.print(f"password:    {payload['password']}", markup=False)
    console.print(f"allow_write: {'on' if state.allow_write else 'off'}", markup=False)
    console.print(f"unlocks:     {_unlock_failures_line(status)}", markup=False)
    console.print(f"devices:     {len(devices)}", markup=False)
    # markup=False everywhere: a User-Agent is the phone's own text, and `x [/b]` in one
    # raised MarkupError here, taking `status` down with it.
    for device in devices:
        state_word = "signed-in" if device.get("signed_in") else "signed-out"
        console.print(
            f"  {device.get('id')}  {str(device.get('ua') or '')[:40]}  "
            f"last {device.get('last_seen')}  expires {device.get('expires_at')}  {state_word}",
            markup=False,
        )


def _unlock_failures_line(status: dict[str, object]) -> str:
    """``0 failed in 30 min``, or the lockout and what to do about it."""
    failed = status.get("failed_unlocks")
    until = status.get("locked_out_until")
    line = f"{failed} failed in 30 min"
    if until:
        line += (
            f" — new unlocks paused until {until}; if that is not you, rotate the link: "
            "aisquare remote regenerate-password --new-link"
        )
    return line


@app.command("allow-write")
def allow_write(
    switch: Annotated[str, typer.Argument(help="on or off (default off; never on by itself).")],
) -> None:
    """Turn the write endpoints on or off for the running/next server."""
    if switch not in ("on", "off"):
        fail(f"say 'on' or 'off', not {switch!r}", error="invalid_switch", ref=switch)
    _remote_runtime().set_allow_write(switch == "on")
    if get_state().json_output:
        typer.echo(json.dumps({"allow_write": switch == "on"}))
    else:
        stdout_console().print(f"✓ write actions {switch}", markup=False)


@app.command("regenerate-password")
def regenerate_password(
    new_link: Annotated[
        bool,
        typer.Option(
            "--new-link",
            help="Also mint a new link: the old one stops working everywhere (it leaked).",
        ),
    ] = False,
    port: LinkPort = DEFAULT_PORT,
) -> None:
    """Mint a new password; every unlocked device has to unlock again."""
    from aisquare.services import remote_server

    state = _remote_runtime()
    password = remote_server.regenerate_password(new_link=new_link)
    payload: dict[str, object] = {"password": password}
    if new_link:
        info = state.connection_info(port)
        payload |= {"token": info.token, "url_local": info.url_local}
    if get_state().json_output:
        typer.echo(json.dumps(payload))
        return
    console = stdout_console()
    console.print(f"✓ new password: {password}", markup=False)
    if new_link:
        console.print(f"✓ new link: {payload['url_local']}", markup=False)
        console.print(
            "  the old link is dead everywhere; a running TUI shows the new one after "
            "Remote is turned off and on",
            markup=False,
        )


@app.command("revoke")
def revoke_command(
    device_id: Annotated[
        str | None, typer.Argument(help="Device id (dev_…, see status).", show_default=False)
    ] = None,
    every: Annotated[
        bool,
        typer.Option("--all", help="Revoke every device; Remote stays on and phones unlock again."),
    ] = False,
) -> None:
    """Drop one device by id, or every device with --all."""
    from aisquare.services import remote_server

    if every == (device_id is not None):
        fail("give one device id, or --all", error="invalid_arguments")
    state = _remote_runtime()
    if device_id is None:  # --all: exactly one of the two was given
        count = state.revoke_every_device("revoked", close_code=remote_server.WS_CLOSE_UNAUTHORIZED)
        if get_state().json_output:
            typer.echo(json.dumps({"revoked_all": count}))
        else:
            stdout_console().print(f"✓ revoked {count} device(s)", markup=False)
        return
    if not remote_server.revoke_remote_device(device_id):
        fail(f"no device {device_id}", error="not_found", ref=device_id)
    if get_state().json_output:
        typer.echo(json.dumps({"revoked": device_id}))
    else:
        stdout_console().print(f"✓ revoked {device_id}", markup=False)


@app.command("needs")
def needs_command() -> None:
    """What needs you right now, across every project: prompts, questions, crashes, limits."""
    from aisquare.services import remote_needs

    payload = remote_needs.needs_cli_payload()
    if get_state().json_output:
        typer.echo(json.dumps(payload))
        return
    items = payload.get("items")
    rows = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    console = stdout_console()
    if not rows:
        console.print("nothing needs you", markup=False)
        return
    now = datetime.now(UTC)
    for item in rows:
        # markup=False: a reason carries labels and roles, which are agents' own text.
        console.print(_needs_line(item, now), markup=False)


def _needs_line(item: dict[str, Any], now: datetime) -> str:
    """``⚑ <kind> · <project> · <agent> — <reason> (<age>)``: one item of the feed."""
    project = item.get("project")
    name = project.get("name") if isinstance(project, dict) else None
    return (
        f"⚑ {item.get('kind') or '?'} · {name or '-'} · {item.get('agent') or '-'}"
        f" — {item.get('reason') or ''} ({_needs_age(item.get('since'), now)})"
    )


def _needs_age(since: object, now: datetime) -> str:
    """How long an item has waited, as the board says it: ``12m``, ``3h05m``."""
    try:
        when = datetime.fromisoformat(str(since))
    except ValueError:
        return "?"
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    minutes = max(0, int((now - when).total_seconds() // 60))
    return f"{minutes}m" if minutes < 60 else f"{minutes // 60}h{minutes % 60:02d}m"
