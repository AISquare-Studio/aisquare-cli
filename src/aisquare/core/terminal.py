"""The terminal a session runs in, named from its environment.

One table (:data:`SIGNS`) for every question asked of the outer terminal: doctor's
fleet-terminal row asks whether it speaks the kitty keyboard protocol
(``services.diagnostics``), and a copy asks what it does with an OSC 52 write
(``core.clipboard.osc52_terminal``). Two tables, one in each, disagreed: the
clipboard's read ``TERM_PROGRAM`` first, so kitty started from a Terminal.app shell
(a stale ``TERM_PROGRAM``, ``KITTY_WINDOW_ID`` set) was taken for Terminal.app
(review of #266, round 1).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

Osc52 = Literal["accepts", "drops", "asks"]
"""What a terminal does with an OSC 52 write as it comes: takes it, ignores it, or takes
it only with a setting of its own on (iTerm2's)."""


@dataclass(frozen=True)
class Sign:
    """One way a terminal shows in the environment, and what is known of it."""

    variable: str
    prefix: str | None
    """The value starts with this; ``None`` for any value."""
    name: str
    kitty: bool
    """Speaks the kitty keyboard protocol: what decides whether shift+enter and the other
    shifted chords reach an agent at all (docs/fleet.md, Keys)."""
    osc52: Osc52 | None = None
    """``None`` where nobody has said."""


SIGNS: tuple[Sign, ...] = (
    Sign("KITTY_WINDOW_ID", None, "kitty", True, "accepts"),
    Sign("GHOSTTY_RESOURCES_DIR", None, "ghostty", True, "accepts"),
    Sign("WEZTERM_EXECUTABLE", None, "wezterm", True, "accepts"),
    Sign("WT_SESSION", None, "Windows Terminal", False, "accepts"),
    Sign("TERM_PROGRAM", "iTerm", "iTerm2", False, "asks"),
    Sign("TERM_PROGRAM", "WezTerm", "wezterm", True, "accepts"),
    Sign("TERM_PROGRAM", "ghostty", "ghostty", True, "accepts"),
    Sign("TERM_PROGRAM", "vscode", "the VS Code terminal", False),
    Sign("TERM_PROGRAM", "Apple_Terminal", "Terminal.app", False, "drops"),
    # An LC_* variable, which macOS's ssh sends by default and the user's tmux keeps.
    Sign("LC_TERMINAL", "iTerm2", "iTerm2", False, "asks"),
    Sign("VTE_VERSION", None, "a VTE terminal (GNOME Terminal, Tilix, …)", False),
    # A terminal's own TERM is the one sign ssh always carries.
    Sign("TERM", "xterm-kitty", "kitty", True, "accepts"),
    Sign("TERM", "xterm-ghostty", "ghostty", True, "accepts"),
    Sign("TERM", "wezterm", "wezterm", True, "accepts"),
    Sign("TERM", "foot", "foot", True, "accepts"),
    Sign("TERM", "alacritty", "alacritty", True, "accepts"),
)
"""How the outer terminal is recognised, first match winning. ``TERM_PROGRAM`` is checked
after the terminal-specific variables because a multiplexer or an IDE can leave an outer
one behind, and ``TERM`` last: anything in between may set it to a name of its own."""


def sign_of(env: Mapping[str, str]) -> Sign | None:
    """The first of :data:`SIGNS` the environment shows; ``None`` for a terminal it names
    not at all."""
    for sign in SIGNS:
        value = env.get(sign.variable, "")
        if value and (sign.prefix is None or value.startswith(sign.prefix)):
            return sign
    return None


def outer_terminal(environ: Mapping[str, str] | None = None) -> tuple[str, bool | None]:
    """``(name, speaks the kitty keyboard protocol)`` for the terminal this shell runs in.

    ``None`` for the protocol when the terminal is not recognised — a guess in either
    direction would send the operator chasing the wrong fix.
    """
    env = os.environ if environ is None else environ
    sign = sign_of(env)
    if sign is not None:
        return sign.name, sign.kitty
    term = env.get("TERM", "").strip()
    return (f"unknown (TERM={term})" if term else "unknown"), None
