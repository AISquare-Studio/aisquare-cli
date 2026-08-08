"""Rich console factories that honour the global output flags."""

from __future__ import annotations

import contextlib
import io
import sys

from rich.console import Console

from aisquare.core.state import get_state


def _ensure_utf8(stream: object) -> None:
    """Make a redirected Windows stream able to carry non-ASCII output.

    Attached to a console, Windows streams are already UTF-8. Redirected to a
    file or a pipe they fall back to the ANSI codepage (cp1252 in most
    installs), which cannot encode the ``✓``/``⚠``/``→`` this CLI prints — so
    ``aisquare doctor > out.txt`` dies with ``UnicodeEncodeError`` while the
    same command run interactively is fine. Reconfiguring the stream keeps the
    intended symbols rather than degrading them to ASCII.
    """
    if sys.platform != "win32" or not isinstance(stream, io.TextIOWrapper):
        return
    if (stream.encoding or "").lower().replace("-", "") == "utf8":
        return
    # A detached or already-closed stream cannot be reconfigured; printing is
    # about to fail anyway, and this must not be what raises.
    with contextlib.suppress(OSError, ValueError):
        stream.reconfigure(encoding="utf-8", errors="replace")


def stdout_console() -> Console:
    """Console for regular program output."""
    _ensure_utf8(sys.stdout)
    return Console(no_color=get_state().no_color, highlight=False, soft_wrap=True)


def stderr_console() -> Console:
    """Console for diagnostics and errors."""
    _ensure_utf8(sys.stderr)
    return Console(stderr=True, no_color=get_state().no_color, highlight=False, soft_wrap=True)
