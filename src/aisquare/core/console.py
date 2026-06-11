"""Rich console factories that honour the global output flags."""

from __future__ import annotations

from rich.console import Console

from aisquare.core.state import get_state


def stdout_console() -> Console:
    """Console for regular program output."""
    return Console(no_color=get_state().no_color, highlight=False, soft_wrap=True)


def stderr_console() -> Console:
    """Console for diagnostics and errors."""
    return Console(stderr=True, no_color=get_state().no_color, highlight=False, soft_wrap=True)
