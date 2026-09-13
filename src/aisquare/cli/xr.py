"""``aisquare xr`` — serve the board as a spatial client on :8748.

``serve`` is the model this follows, deliberately and in detail: the same
dependency guard shape, the same explicit project activation, the same
``--show-token`` idiom, the same bearer token. What differs is who is on the
other end. ``serve`` speaks MCP to agent clients on 8747; ``xr`` serves a
static WebXR client and one websocket to a human in a headset on 8748.
"""

from __future__ import annotations

import json
from typing import Annotated

import typer

from aisquare.cli.common import fail
from aisquare.core.console import stderr_console, stdout_console
from aisquare.core.state import get_state

_INSTALL_HINT = "pip install 'aisquare-cli[xr]'"

#: The modules ``services.xr.server`` actually imports at serve time. Probed
#: rather than imported so a missing extra is the CLI's error contract instead
#: of a ``ModuleNotFoundError`` out of uvicorn several frames later — the same
#: reasoning as ``cli/serve.py``'s ``REQUIRED_MODULE``, and the same failure it
#: prevents. ``websockets`` is on the list because uvicorn serves HTTP happily
#: without it and answers every websocket handshake with 404: the page would
#: load and the ring would never populate, which is the worst way to learn a
#: dependency is missing.
REQUIRED_MODULES = ("starlette", "uvicorn", "websockets")

XR_PORT = 8748
"""``serve`` owns 8747 (plan §2.5). Neighbours, not roommates."""


def _find_spec(name: str) -> object | None:
    """Indirection so the dependency state can be exercised in tests.

    Patching ``importlib.util.find_spec`` itself would sabotage every other
    import for the duration of the test, including pytest's own.
    """
    import importlib.util

    return importlib.util.find_spec(name)


def _dependency_error() -> str | None:
    """Why the XR server cannot start here, or ``None`` when it can."""
    missing = []
    for name in REQUIRED_MODULES:
        try:
            if _find_spec(name) is None:
                missing.append(name)
        except (ImportError, ValueError):
            missing.append(name)
    if not missing:
        return None
    return f"the xr extra is not installed (missing {', '.join(missing)}) — {_INSTALL_HINT}"


def _url(port: int, token: str) -> str:
    """The URL to open, token and all.

    The token rides in the FRAGMENT, not the query string: a fragment is never
    sent to the server, never lands in an access log, and is not what a
    referrer header carries. It is also the only half of a URL a single-page
    client can read and then erase from the address bar.

    ``localhost`` regardless of ``--bind``, because that is the origin the
    headset must use: ``navigator.xr`` exists only in a secure context, and
    ``http://`` is secure on loopback and nowhere else. ``adb reverse`` is what
    makes the headset's ``localhost`` this machine's port (plan §13).
    """
    return f"http://localhost:{port}/#token={token}"


def _adb_reverse(port: int) -> str:
    return f"adb reverse tcp:{port} tcp:{port}"


def _announce(port: int, token: str, bind: str) -> None:
    """Everything an operator needs to get a headset onto this board.

    On stderr so ``--json`` stdout stays machine-readable, and printed on every
    start rather than hidden behind a flag: the two instructions below are the
    ones plan §13 says will otherwise eat the first hour, and an instruction
    nobody is shown is an instruction nobody follows.
    """
    console = stderr_console()
    console.print(f"Open in the headset browser:  {_url(port, token)}")
    console.print(f"Tether first (USB):           {_adb_reverse(port)}")
    console.print(
        "  `adb reverse` is what makes the headset's localhost this port, and localhost "
        "is the only http:// origin WebXR treats as secure — over the LAN address "
        "instead, navigator.xr is undefined and the session request fails."
    )
    console.print(
        f"  Untethered, add the LAN origin (http://<this machine>:{port}) under "
        'chrome://flags → "Insecure origins treated as secure" in the Quest browser, '
        "and relaunch it."
    )
    console.print(
        "  Bookmark the URL the first time. Typing it again in a headset is its own "
        "small punishment."
    )
    if bind not in ("127.0.0.1", "localhost", "::1"):
        # Spelled out rather than interpolated inline: a backslash inside an
        # f-string expression is a syntax error before 3.12, and this package
        # supports 3.11.
        shown = bind or '""'
        console.print(
            f"--bind {shown} is not loopback: the token is the only gate, and it "
            "crosses the wire in clear over plain HTTP. Trusted networks only."
        )


def xr(
    port: Annotated[
        int, typer.Option("--port", help="HTTP port.", envvar="AISQUARE_XR_PORT")
    ] = XR_PORT,
    bind: Annotated[
        str,
        typer.Option(
            "--bind",
            help="HTTP bind address. Loopback is the right answer with `adb reverse`; "
            "a LAN bind needs the chrome://flags secure-origin exception in the headset.",
        ),
    ] = "127.0.0.1",
    show_token: Annotated[
        bool,
        typer.Option("--show-token", help="Print the connection details and exit."),
    ] = False,
) -> None:
    """Serve the agent board as a spatial client for a WebXR headset."""
    problem = _dependency_error()
    if problem is not None:
        fail(problem, error="xr_not_installed")
    from aisquare.services import mcp_server

    # One token for both servers, from the same 0600 file: an operator who has
    # already wired up `serve` has already wired up this. `mcp_server` imports
    # `mcp` lazily, so reading the token here costs nothing and works without
    # the serve extra installed at all.
    token = mcp_server.serve_token()

    if show_token:
        if get_state().json_output:
            typer.echo(
                json.dumps(
                    {
                        "url": _url(port, token),
                        "token": token,
                        "bind": bind,
                        "adb_reverse": _adb_reverse(port),
                    }
                )
            )
        else:
            console = stdout_console()
            console.print(f"URL:   {_url(port, token)}")
            console.print(f"Token: {token}")
            console.print(f"Adb:   {_adb_reverse(port)}")
        return

    from aisquare.services.xr import server as xr_server

    # Before activation and before uvicorn: an occupied port is the single most
    # likely way this command fails (the operator already has one running), and
    # it should cost a sentence, not a traceback — and certainly not a project
    # activated by a command that then died.
    if xr_server.port_in_use(bind, port):
        fail(
            f"port {port} is in use on {bind} — stop the other server or use --port",
            error="xr_port_busy",
        )

    # Starting a server here IS the opt-in for this project, exactly as it is
    # for `serve`: activate explicitly and visibly, so nothing activates a
    # directory as a side effect of a browser connecting to it.
    from aisquare.cli.team import STORE_ERRORS, _fail_team
    from aisquare.services import team as team_service

    try:
        project = team_service.activate()
    except STORE_ERRORS as exc:
        _fail_team(exc)

    stderr_console().print(
        f"Serving the spatial board for {project.root.name or project.id} on "
        f"{bind}:{port}. Ctrl-C stops."
    )
    _announce(port, token, bind)
    xr_server.run(project, bind=bind, port=port, token=token)
