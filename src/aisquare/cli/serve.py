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

#: The module ``services.mcp_server.build_server`` actually imports. Probing the
#: DISTRIBUTION instead (``find_spec("mcp")``) is not enough: mcp 2.0.0 ships a
#: package called ``mcp`` that no longer contains this module, so the
#: distribution check passes and the user gets a raw ModuleNotFoundError from
#: deep inside the server instead of the CLI's error contract. A test pins this
#: name against the import in mcp_server.py so the two cannot drift.
REQUIRED_MODULE = "mcp.server.fastmcp"


def _find_spec(name: str) -> object | None:
    """Indirection so the dependency state can be exercised in tests.

    Patching ``importlib.util.find_spec`` itself would sabotage every other
    import for the duration of the test, including pytest's own.
    """
    import importlib.util

    return importlib.util.find_spec(name)


def _dependency_error() -> str | None:
    """Why the MCP server cannot start here, or ``None`` when it can.

    Two failures that need two different fixes: the extra is missing (install
    it), or the installed ``mcp`` is a major that deleted what we import
    (pin it back). Telling someone to install a package they already have is
    worse than saying nothing, so they are reported separately.
    """
    try:
        if _find_spec(REQUIRED_MODULE) is not None:
            return None
    except (ImportError, ValueError):
        # find_spec on a dotted name imports its parents first, so an absent
        # `mcp` raises here rather than returning None. That IS the
        # extra-not-installed case, and it falls through to the check below.
        pass
    if _find_spec("mcp") is None:
        return f"the serve extra is not installed — {_INSTALL_HINT}"
    return (
        f"the installed mcp package has no {REQUIRED_MODULE}{_installed_mcp()} — "
        "aisquare needs mcp>=1.10,<2, which is where that module lives. "
        "Pin it back: pip install 'mcp>=1.10,<2'"
    )


def _installed_mcp() -> str:
    """``" (mcp 2.0.0)"`` when the version is knowable, else nothing."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return f" (mcp {version('mcp')})"
    except PackageNotFoundError:  # importable but not an installed distribution
        return ""


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
    # mcp_server itself imports lazily, so probe the dependency directly —
    # a bare `import` succeeds without the extra and dies later mid-request.
    problem = _dependency_error()
    if problem is not None:
        fail(problem, error="serve_not_installed")
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
    from aisquare.cli.team import STORE_ERRORS, _fail_team
    from aisquare.core.orchestrator import team_project
    from aisquare.core.workspace import find_project_root
    from aisquare.services import team as team_service

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
        except STORE_ERRORS as exc:
            # team_disabled, but also a wedged store or a failed activation
            # read-back — serve must exit with the error contract, never a
            # traceback (#20 review).
            _fail_team(exc)
        # stdout is the MCP protocol channel; announce on stderr so the
        # opt-in is never invisible.
        stderr_console().print(
            f"aisquare agent orchestrator activated for {project.root} (stdio serve)"
        )
        mcp_server.run_stdio(close_after=close_after)
        return
    try:
        project = team_service.activate()
    except STORE_ERRORS as exc:
        _fail_team(exc)
    stderr_console().print(
        f"Serving the orchestrator for {project.root.name or project.id} at "
        f"http://{bind}:{port}/mcp "
        "(bearer token required — see `aisquare serve --show-token`). Ctrl-C stops."
    )
    mcp_server.run_http(bind=bind, port=port)
