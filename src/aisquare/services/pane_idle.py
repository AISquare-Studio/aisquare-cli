"""Whether an agent's pane shows it idle at its prompt — the screen's word (#123).

The board says ``working`` from a prompt until the turn's ``Stop`` hook. A turn ended
with Esc fires no Stop, and before #123 a session start wrote ``working`` too, so the
fleet trusted a row that said busy for its whole fresh window while the agent sat at an
empty prompt. ``services.fleet._derive`` asks here, once, only for a fresh ``working``
row whose pane has printed nothing for ``[fleet] busy_quiet_seconds`` — Claude Code
redraws its spinner every second during a turn, so a quiet pane is the exception — and
the pane's own agent decides, as its adapter reads its screen (``services.captain.screen
.ready``: Claude Code's input box drawn and idle, ``claude_screen.box_idle``; another
agent's own ready prompt), the reader the captain's own delivery uses.
"""

from __future__ import annotations

from aisquare.core.tmux import TmuxServer
from aisquare.models import FleetAgent
from aisquare.services.captain import screen


def box_idle_now(server: TmuxServer, pane_id: str, agent: FleetAgent) -> bool:
    """One capture of ``pane_id``: ``agent``'s prompt is drawn and idle, as its own agent
    reads it (an agent this install does not know is never idle on its screen's word).

    FAILS CLOSED to "not idle": a capture that cannot be made, or a screen with no idle
    box (a dialog, a spinner, another program), leaves the row the board's ``working``,
    as before #123. Reading busy as idle would let a ``fleet tell`` type into a turn.
    """
    try:
        frame = server.capture(pane_id)
    except Exception:
        return False
    return screen.ready(agent, frame.lines)
