"""The terminal a session runs in, named from its environment.

One table (:data:`SIGNS`) for every question asked of the outer terminal: doctor's
fleet-terminal row asks whether it speaks the kitty keyboard protocol
(``services.diagnostics``), a copy asks what it does with an OSC 52 write
(``core.clipboard.osc52_terminal``), and the help asks whether the user types on a Mac
(:func:`mac_keyboard`). Two tables, one in each, disagreed: the
clipboard's read ``TERM_PROGRAM`` first, so kitty started from a Terminal.app shell
(a stale ``TERM_PROGRAM``, ``KITTY_WINDOW_ID`` set) was taken for Terminal.app
(review of #266, round 1).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from aisquare.core.browser import REMOTE_MARKERS

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
    mac: bool = False
    """Only ever runs on a Mac, so the user's keyboard is a Mac's (:func:`mac_keyboard`)."""


SIGNS: tuple[Sign, ...] = (
    Sign("KITTY_WINDOW_ID", None, "kitty", True, "accepts"),
    Sign("GHOSTTY_RESOURCES_DIR", None, "ghostty", True, "accepts"),
    Sign("WEZTERM_EXECUTABLE", None, "wezterm", True, "accepts"),
    Sign("WT_SESSION", None, "Windows Terminal", False, "accepts"),
    Sign("TERM_PROGRAM", "iTerm", "iTerm2", False, "asks", mac=True),
    Sign("TERM_PROGRAM", "WezTerm", "wezterm", True, "accepts"),
    Sign("TERM_PROGRAM", "ghostty", "ghostty", True, "accepts"),
    Sign("TERM_PROGRAM", "vscode", "the VS Code terminal", False),
    Sign("TERM_PROGRAM", "Apple_Terminal", "Terminal.app", False, "drops", mac=True),
    # An LC_* variable, which macOS's ssh sends by default. A tmux keeps the one of
    # whoever started it, so behind a multiplexer it names nobody (`multiplexed`).
    Sign("LC_TERMINAL", "iTerm2", "iTerm2", False, "asks", mac=True),
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


def multiplexed(env: Mapping[str, str]) -> bool:
    """Whether tmux or screen sits between this session and the terminal.

    The terminal's own variables are then those of whichever terminal started the
    multiplexer, kept for as long as it runs: tmux refreshes only its
    ``update-environment`` list on an attach, and ``LC_TERMINAL`` is not on it. Started
    from iTerm2 over SSH and reattached from kitty, it still says iTerm2 (review of #266,
    round 2), so behind one no sign names the terminal attached now.
    """
    term = env.get("TERM", "")
    return bool(
        env.get("TERM_PROGRAM") == "tmux"
        or env.get("TMUX")
        or env.get("STY")
        or term.startswith(("tmux", "screen"))
    )


def mac_keyboard(environ: Mapping[str, str] | None = None, *, platform: str | None = None) -> bool:
    """Whether the user types on a Mac, as far as the session can tell: what decides
    whether the help names ⌘C, ⌘V and ⌥-drag.

    The terminal is a Mac's own (Terminal.app, iTerm2 — iTerm2 over SSH too, by its
    ``LC_TERMINAL``), or this machine is a Mac and the session is local. The host's
    ``sys.platform`` alone was the wrong machine both ways: a Mac user SSHed into Linux
    (where OSC 52 is the only route, and the keys matter most) never saw the hints,
    and a Linux client SSHed into a Mac did (review of #266, round 1). Terminal.app
    over SSH names itself to nothing, so its user gets the plain help. Behind tmux or
    screen the sign may be another terminal's (:func:`multiplexed`), so only a local
    session on a Mac says so there.
    """
    env = os.environ if environ is None else environ
    sign = sign_of(env)
    if sign is not None and sign.mac and not multiplexed(env):
        return True
    system = sys.platform if platform is None else platform
    return system == "darwin" and not any(env.get(name) for name in REMOTE_MARKERS)
