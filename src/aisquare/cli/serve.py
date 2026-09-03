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
#: DISTRIBUTION instead (``find_spec("mcp")``) is not enough: mcp 1.x ships a
#: package called ``mcp`` that does not contain this module (it had
#: ``mcp.server.fastmcp``, which 2.0.0 renamed to this), so the distribution
#: check passes and the user gets a raw ModuleNotFoundError from deep inside
#: the server instead of the CLI's error contract. A test pins this name
#: against the import in mcp_server.py so the two cannot drift.
REQUIRED_MODULE = "mcp.server.mcpserver"


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
    it), or the installed ``mcp`` is a major without what we import (move it
    into range — today that means a 1.x, which predates the rename). Telling
    someone to install a package they already have is worse than saying
    nothing, so they are reported separately.
    """
    try:
        if _find_spec(REQUIRED_MODULE) is not None:
            return None
    except (ImportError, ValueError) as exc:
        # find_spec on a dotted name imports its parents first, so an absent
        # `mcp` raises here rather than returning None. That IS the
        # extra-not-installed case, and it falls through to the check below.
        # A parent that exists but will not import raises too — mcp present,
        # one of ITS dependencies missing or broken — and that is a third
        # case: neither "install the extra" nor "move mcp into range" fixes
        # it, and the second would send someone to install the mcp they have.
        # The exception names the module that failed; a bare re-raise from a
        # test stub does not, and takes the absent-mcp path as before.
        if getattr(exc, "name", None) not in (None, "mcp"):
            return (
                f"mcp{_installed_mcp()} is installed but cannot be imported — {exc}. "
                f"Reinstall the serve extra: {_INSTALL_HINT}"
            )
    if _find_spec("mcp") is None:
        return f"the serve extra is not installed — {_INSTALL_HINT}"
    return (
        f"the installed mcp package has no {REQUIRED_MODULE}{_installed_mcp()} — "
        "aisquare needs mcp>=2.1,<3, which is where that module lives. "
        "Move it into range: pip install 'mcp>=2.1,<3'"
    )


def _installed_mcp() -> str:
    """``" (mcp 1.29.1)"`` when the version is knowable, else nothing.

    Today only a 1.x reaches the out-of-range message — every 2.x release
    ships the probed module — but a 3.x would, and a 2.x that will not import
    reaches the reinstall message, which this decorates too.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return f" (mcp {version('mcp')})"
    except PackageNotFoundError:  # importable but not an installed distribution
        return ""


_WILDCARD_BINDS = frozenset({"0.0.0.0", "::", ""})


def _client_url(bind: str, port: int) -> str:
    """The URL a client dials for ``bind`` — a listen address is not always one.

    An IPv6 literal needs brackets: ``http://::1:8747/mcp`` is not a URL, and
    ``::1`` is one of the spellings that keeps the transport's Host validation,
    so it has to print. A wildcard bind names every interface and no
    destination, so this machine's hostname stands in for it.
    """
    import socket

    host = socket.gethostname() if bind in _WILDCARD_BINDS else bind
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}/mcp"


def serve(
    stdio: Annotated[
        bool,
        typer.Option("--stdio", help="Serve over stdio (for Claude Desktop / wsl.exe launch)."),
    ] = False,
    port: Annotated[
        int, typer.Option("--port", help="HTTP port.", envvar="AISQUARE_SERVE_PORT")
    ] = 8747,
    bind: Annotated[
        str,
        typer.Option(
            "--bind",
            help="HTTP bind address. 127.0.0.1, localhost or ::1 keep the transport's "
            "Host/Origin validation; any other bind runs with the bearer token as the only "
            "gate, sent in clear — trusted networks or a TLS proxy only.",
        ),
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
        url = _client_url(bind, port)
        if get_state().json_output:
            typer.echo(json.dumps({"url": url, "token": token, "bind": bind}))
        else:
            console = stdout_console()
            console.print(f"URL:    {url}")
            if bind in _WILDCARD_BINDS:
                console.print(f"Bind:   {bind} (every interface — the URL names this machine)")
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
        f"{_client_url(bind, port)} "
        "(bearer token required — see `aisquare serve --show-token`). Ctrl-C stops."
    )
    if bind not in mcp_server.LOOPBACK_BINDS:
        # The one HTTP behaviour mcp 2 changed, said where the operator opening
        # the port can see it: neither the SDK nor anything else says what a
        # non-loopback bind gives up. Keyed on the same tuple the server keys
        # on, so the notice cannot drift from what actually happens.
        stderr_console().print(
            f"--bind {bind} is not one of {', '.join(mcp_server.LOOPBACK_BINDS)}: the MCP "
            "transport's Host/Origin validation is off for it, and the bearer token is the "
            "only gate — a long-lived credential sent in clear over plain HTTP on every "
            "request. Keep this on a trusted network, or behind a TLS-terminating proxy."
        )
    mcp_server.run_http(bind=bind, port=port)
