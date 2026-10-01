"""``python -m aisquare.services.captain --stdio`` — the Actions server without the CLI tree.

The same server as ``aisquare captain serve --stdio``, started without
importing every CLI command first: measured on a box at load 5, 0.80 s to the
``initialize`` answer against 1.0 s through the CLI (most of what is left is
the mcp SDK's own import). How the captain session mounts the server is the
spawn card's choice (T2); both entries serve the same tools.
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m aisquare.services.captain",
        description="The captain's Actions MCP server (the same as `aisquare captain serve`).",
    )
    parser.add_argument("--stdio", action="store_true", help="serve over stdio (required)")
    parser.add_argument(
        "--close-after",
        type=int,
        default=300,
        help="exit after this many seconds without a client message or a running tool call "
        "(0 = run forever)",
    )
    args = parser.parse_args(argv)
    if not args.stdio:
        parser.error("the captain's server speaks stdio only — pass --stdio")
    if args.close_after < 0:
        parser.error("--close-after cannot be negative")
    try:
        import mcp.server.mcpserver  # noqa: F401  (the module the server builds on)
    except ImportError as exc:
        print(
            f"the MCP server cannot start: {exc} — pip install 'aisquare-cli[serve]' "
            "(aisquare captain serve --stdio says more)",
            file=sys.stderr,
        )
        return 1
    from aisquare.core.orchestrator import team_enabled

    if not team_enabled():
        print(
            "the agent orchestrator is disabled (AISQUARE_TEAM=0) — every captain action goes "
            "through the board, so the captain cannot serve without it",
            file=sys.stderr,
        )
        return 1
    from aisquare.services.captain import actions

    actions.run_stdio(close_after=args.close_after)
    return 0


if __name__ == "__main__":
    sys.exit(main())
