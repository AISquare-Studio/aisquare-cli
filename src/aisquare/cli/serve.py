"""``aisquare serve`` — expose the team bus to remote agents over MCP."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from aisquare.cli.common import fail
from aisquare.core.console import stderr_console, stdout_console
from aisquare.core.state import get_state

_INSTALL_HINT = "pip install 'aisquare-cli[serve]' (or: pipx inject aisquare-cli mcp)"


def serve(
    stdio: Annotated[
        bool,
        typer.Option("--stdio", help="Serve over stdio (for Claude Desktop / wsl.exe launch)."),
    ] = False,
    port: Annotated[
        int, typer.Option("--port", help="HTTP port.", envvar="AISQUARE_SERVE_PORT")
    ] = 8747,
    bind: Annotated[
        str, typer.Option("--bind", help="HTTP bind address (keep it loopback unless you must).")
    ] = "127.0.0.1",
    show_token: Annotated[
        bool,
        typer.Option("--show-token", help="Print the HTTP connection details and exit."),
    ] = False,
) -> None:
    """Run the team-bus MCP server so remote Claude clients can join this project."""
    try:
        from aisquare.services import mcp_server
    except ImportError:
        fail(f"the serve extra is not installed — {_INSTALL_HINT}", error="serve_not_installed")
    if show_token:
        token = mcp_server.serve_token()
        if get_state().json_output:
            typer.echo(json.dumps({"url": f"http://{bind}:{port}/mcp", "token": token}))
        else:
            console = stdout_console()
            console.print(f"URL:    http://{bind}:{port}/mcp")
            console.print(f"Header: Authorization: Bearer {token}")
        return
    if stdio:
        mcp_server.run_stdio()
        return
    stderr_console().print(
        f"Serving the team bus at http://{bind}:{port}/mcp "
        "(bearer token required — see `aisquare serve --show-token`). Ctrl-C stops."
    )
    mcp_server.run_http(bind=bind, port=port)
