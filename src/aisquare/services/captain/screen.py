"""What an agent's pane SHOWS, as the pane's own agent reads it.

One reader for the things that type into a pane (board 13278): ``brain.say``, which must
never type into a dialog (13227), and ``actions.press`` and ``paste``, which answer one.
Two parsers of the same screen could disagree on one pane — ``say`` refusing while
``press`` answers, or the reverse — so both ask here.

Each question goes to the agent of the pane's OWN row, as its launch record places it
(``fleet.placed_row``: its agent the record's, never the store's column), through its
adapter's screen (``AgentAdapter.screen``, a ``types.AgentScreen``): ``screen_prompt`` (a
prompt at the bottom of the screen, and the keys that answer it), ``ready`` (a typed line
lands as a prompt there: its ``ready_on_screen``, else its default), ``drawn`` (its prompt
drawn, busy or not: ``prompt_on_screen``) and ``modal`` (what a typed line must not go into:
``modal_on_screen``). Claude Code's are ``core.agent_adapters.claude_screen``'s: its
input box, chooser, trust dialog and rating survey. Codex's are its own dialogs: its
folder trust, the owner's; its approval view, a chooser on ``1`` and Esc; its empty
composer. Read with another agent's shapes, a Codex pane at its folder-trust dialog
showed no prompt at all, and a paste's Enter there picked "Trust and continue".

An agent this install does not know, or one that reads none of these off its screen, shows
no prompt and is never ready: nothing is typed on its screen's word.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from aisquare.core.agent_adapters import recorded_adapter
from aisquare.core.agent_adapters.types import AgentScreen, ScreenPrompt, screen_of
from aisquare.models import FleetAgent

PANE_ESCAPES = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
"""Every escape a captured pane row can carry — CSI with any parameters (colon ones
included), OSC (hyperlinks, ended by BEL or by ST, which is ESC and ONE backslash) and the
bare two-byte sequences. T1's pattern, character for character (coderp's S1 on #219)."""


def strip_escapes(lines: Sequence[str]) -> list[str]:
    """The rows as the owner sees them: escapes gone, blank tail dropped."""
    rows = [PANE_ESCAPES.sub("", line).rstrip() for line in lines]
    while rows and not rows[-1].strip():
        rows.pop()
    return rows


def _screen(agent: FleetAgent) -> AgentScreen | None:
    """How ``agent``'s own agent reads its screen (``AgentAdapter.screen``, its defaults where
    it declares none); ``None`` for one this install does not know."""
    adapter = recorded_adapter(agent.agent)
    return screen_of(adapter) if adapter is not None else None


def reads_prompts(agent: FleetAgent) -> bool:
    """Whether ``agent``'s screen is read for a prompt (``AgentScreen.screen_prompt``): where
    it is not, nothing can tell its folder-trust dialog from its prompt."""
    screen = _screen(agent)
    return screen is not None and screen.screen_prompt is not None


def prompt_showing(agent: FleetAgent, lines: Sequence[str]) -> ScreenPrompt | None:
    """The prompt at the bottom of ``agent``'s pane (``lines``, as captured), read by its own
    agent, or ``None``."""
    screen = _screen(agent)
    read = screen.screen_prompt if screen is not None else None
    return read(strip_escapes(lines)) if read is not None else None


def ready(agent: FleetAgent, lines: Sequence[str]) -> bool:
    """Whether ``agent``'s pane shows its prompt drawn with no turn running at it, so a typed
    line lands as a prompt (``AgentScreen.ready``)."""
    screen = _screen(agent)
    return screen is not None and screen.ready(strip_escapes(lines))


def drawn(agent: FleetAgent, lines: Sequence[str]) -> bool:
    """Whether ``agent``'s pane shows its prompt drawn, a turn running at it or not
    (``AgentScreen.drawn``)."""
    screen = _screen(agent)
    return screen is not None and screen.drawn(strip_escapes(lines))


def modal_showing(agent: FleetAgent, lines: Sequence[str]) -> str | None:
    """What ``agent``'s pane shows that text must not be typed into, in the owner's words
    (``AgentScreen.modal``), or ``None``."""
    screen = _screen(agent)
    return screen.modal(strip_escapes(lines)) if screen is not None else None
