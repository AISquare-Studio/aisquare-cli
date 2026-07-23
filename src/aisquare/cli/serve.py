"""``aisquare serve`` — expose the orchestrator to remote agents over MCP."""

from __future__ import annotations

import json
import os
from pathlib import Path
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
    close_after: Annotated[
        int,
        typer.Option(
            "--close-after",
            min=0,
            envvar="AISQUARE_SERVE_CLOSE_AFTER",
            help="stdio only: exit after this many seconds without a client message "
            "(default 300; 0 = run forever — set it for persistent clients like "
            "Claude Desktop). HTTP mode ignores it.",
        ),
    ] = 300,
) -> None:
    """Run the orchestrator MCP server so remote Claude clients can join this project."""
    import importlib.util

    # mcp_server itself imports lazily, so probe the dependency directly —
    # a bare `import` succeeds without the extra and dies later mid-request.
    if importlib.util.find_spec("mcp") is None:
        fail(f"the serve extra is not installed — {_INSTALL_HINT}", error="serve_not_installed")
    from aisquare.services import mcp_server

    if show_token:
        token = mcp_server.serve_token()
        if get_state().json_output:
            typer.echo(json.dumps({"url": f"http://{bind}:{port}/mcp", "token": token}))
        else:
            console = stdout_console()
            console.print(f"URL:    http://{bind}:{port}/mcp")
            console.print(f"Header: Authorization: Bearer {token}")
        return
    # Starting a server here IS the opt-in for this project: activate it
    # explicitly (and visibly — `team on` semantics, with the pipe event),
    # so remote calls never activate a directory as a side effect.
    from aisquare.core.orchestrator import team_project
    from aisquare.core.workspace import find_project_root
    from aisquare.services import team as team_service
    from aisquare.services.team import TeamDisabledError

    if stdio:
        # Claude Desktop launches stdio servers from wherever it likes
        # ($HOME by default) and find_project_root falls back to the cwd —
        # refuse to silently activate a directory that is not a project.
        target = team_project(None)
        marker_root = find_project_root(Path.cwd())
        if not os.environ.get("AISQUARE_TEAM_HUB", "").strip() and not any(
            (marker_root / marker).exists() for marker in (".git", ".hg", ".aisquare")
        ):
            fail(
                f"refusing to activate {target.root}: not a project root (no .git/.hg/"
                ".aisquare marker). Launch from the repo, or set AISQUARE_TEAM_HUB.",
                error="not_a_project",
            )
        try:
            project = team_service.activate()
        except TeamDisabledError as exc:
            fail(str(exc), error="team_disabled")
        # stdout is the MCP protocol channel; announce on stderr so the
        # opt-in is never invisible.
        stderr_console().print(
            f"aisquare agent orchestrator activated for {project.root} (stdio serve)"
        )
        mcp_server.run_stdio(close_after=close_after)
        return
    try:
        project = team_service.activate()
    except TeamDisabledError as exc:
        fail(str(exc), error="team_disabled")
    stderr_console().print(
        f"Serving the orchestrator for {project.root.name or project.id} at "
        f"http://{bind}:{port}/mcp "
        "(bearer token required — see `aisquare serve --show-token`). Ctrl-C stops."
    )
    mcp_server.run_http(bind=bind, port=port)
