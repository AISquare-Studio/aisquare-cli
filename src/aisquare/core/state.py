"""Process-wide runtime state derived from the global CLI flags."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RuntimeState:
    """Global flags for the current invocation, set by the root CLI callback."""

    verbose: bool = False
    quiet: bool = False
    json_output: bool = False
    profile: str = "default"
    no_color: bool = False


_state = RuntimeState()


def get_state() -> RuntimeState:
    """Return the runtime state for the current invocation."""
    return _state


def set_state(state: RuntimeState) -> None:
    """Install ``state`` as the current invocation's runtime state."""
    global _state
    _state = state


def reset_state() -> None:
    """Restore the default runtime state (used between tests)."""
    set_state(RuntimeState())
