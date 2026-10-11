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


FLAGS: dict[str, str] = {
    "no_color": "--no-color",
    "verbose": "--verbose",
    "quiet": "--quiet",
    "json_output": "--json",
    "profile": "--profile",
}
"""The flag that sets each :class:`RuntimeState` field, by field.

The root callback (``cli.app.main_callback``) and every command
(``cli.global_flags``) declare them, and ``selfcli.global_flags`` gives them back to
the command it hands the terminal to. tests/test_global_flags.py ties all three to
this table and this table to the fields, so a flag added to the first two and left
out here fails there, not by being dropped on every hand-over."""


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
