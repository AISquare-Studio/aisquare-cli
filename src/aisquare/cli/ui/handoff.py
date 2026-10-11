"""asq handing its terminal over (Update and Uninstall in the Doctor): what stands down.

The shell refuses to hand over under a worker someone asked to write, and waits out
every other thread worker still running (``FleetApp.on_doctor_view_hand_off``). A
worker started after its last look would be cut off by the quit, so every start that
runs on asq's own schedule (a tick, a page's mount or show, a tab, a keystroke) asks
:func:`handing_over` first and stands down.
"""

from __future__ import annotations

from textual.dom import DOMNode


def handing_over(node: DOMNode) -> bool:
    """Whether the app ``node`` runs in is quitting to hand its terminal over.

    Read with getattr: a view also runs under an app with no hand-over at all
    (``aisquare board -w``'s, a test's host).
    """
    return getattr(node.app, "hand_off", None) is not None
