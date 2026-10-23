"""What an agent's pane SHOWS, as the pane's own agent reads it.

One reader for the things that type into a pane (board 13278): ``brain.say``, which must
never type into a dialog (13227), and ``actions.press`` and ``paste``, which answer one.
Two parsers of the same screen could disagree on one pane — ``say`` refusing while
``press`` answers, or the reverse — so both ask here.

Each question goes to the agent of the pane's OWN row, as its launch record places it
(``fleet.placed_row``: its agent the record's, never the store's column), through its
adapter's capabilities: ``screen_prompt`` (a prompt at the bottom of the screen, and the
keys that answer it), ``ready_on_screen`` (a typed line lands as a prompt there,
``types.screen_ready``) and ``modal_on_screen`` (what a typed line must not go into,
``types.screen_modal``). Claude Code's are ``core.agent_adapters.claude_screen``'s: its
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
from aisquare.core.agent_adapters.types import (
    AgentCapabilities,
    ScreenPrompt,
    screen_modal,
    screen_ready,
)
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


def _capabilities(agent: FleetAgent) -> AgentCapabilities | None:
    """What ``agent``'s own agent reads off its screen; ``None`` for one this install does
    not know."""
    adapter = recorded_adapter(agent.agent)
    return adapter.capabilities if adapter is not None else None


def reads_prompts(agent: FleetAgent) -> bool:
    """Whether ``agent``'s screen is read for a prompt (``capabilities.screen_prompt``): where
    it is not, nothing can tell its folder-trust dialog from its prompt."""
    capabilities = _capabilities(agent)
    return capabilities is not None and capabilities.screen_prompt is not None


def prompt_showing(agent: FleetAgent, lines: Sequence[str]) -> ScreenPrompt | None:
    """The prompt at the bottom of ``agent``'s pane (``lines``, as captured), read by its own
    agent, or ``None``."""
    capabilities = _capabilities(agent)
    read = capabilities.screen_prompt if capabilities is not None else None
    return read(strip_escapes(lines)) if read is not None else None


def ready(agent: FleetAgent, lines: Sequence[str]) -> bool:
    """Whether ``agent``'s pane shows its prompt drawn with no turn running at it, so a typed
    line lands as a prompt (``types.screen_ready``)."""
    capabilities = _capabilities(agent)
    return capabilities is not None and screen_ready(capabilities, strip_escapes(lines))


def modal_showing(agent: FleetAgent, lines: Sequence[str]) -> str | None:
    """What ``agent``'s pane shows that text must not be typed into, in the owner's words
    (``types.screen_modal``), or ``None``."""
    capabilities = _capabilities(agent)
    return screen_modal(capabilities, strip_escapes(lines)) if capabilities else None
