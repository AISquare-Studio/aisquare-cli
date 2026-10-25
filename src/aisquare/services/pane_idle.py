"""Whether an agent's pane shows Claude Code idle at its prompt — the screen's word (#123).

The board says ``working`` from a prompt until the turn's ``Stop`` hook. A turn ended
with Esc fires no Stop, and before #123 a session start wrote ``working`` too, so the
fleet trusted a row that said busy for its whole fresh window while the agent sat at an
empty prompt. ``services.fleet._derive`` asks here, once, only for a fresh ``working``
row whose pane has printed nothing for ``[fleet] busy_quiet_seconds`` — Claude Code
redraws its spinner every second during a turn, so a quiet pane is the exception — and
the input box decides (``services.captain.screen.box_idle``, the reader the captain's
own delivery uses).
"""

from __future__ import annotations

from aisquare.core.tmux import TmuxServer
from aisquare.services.captain import screen


def box_idle_now(server: TmuxServer, pane_id: str) -> bool:
    """One capture of ``pane_id``: its Claude Code input box is drawn and idle.

    FAILS CLOSED to "not idle": a capture that cannot be made, or a screen with no idle
    box (a dialog, a spinner, another program), leaves the row the board's ``working``,
    as before #123. Reading busy as idle would let a ``fleet tell`` type into a turn.
    """
    try:
        frame = server.capture(pane_id)
    except Exception:
        return False
    return screen.box_idle(frame.lines)
