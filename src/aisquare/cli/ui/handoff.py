"""asq handing its terminal over (Update and Uninstall in the Doctor): what stands down.

The shell refuses to hand over under a worker someone asked to write, and waits out
every other thread worker still running (``FleetApp.on_doctor_view_hand_off``). A
worker started after its last look would be cut off by the quit, so every start that
runs on asq's own schedule (a tick, a page's mount or show, a tab, a keystroke) asks
:func:`handing_over` first and stands down.

A hand-over kept waiting can be called off, and asq carries on, so a start that stood
down is owed, not dropped: it hands :func:`handing_over` itself, and the call-off runs
each owed start once (``FleetApp.owe``). Only a start a tick of its own asks again soon
owes nothing (the board's account names, each board tick).
"""

from __future__ import annotations

from collections.abc import Callable

from textual.dom import DOMNode


def handing_over(node: DOMNode, owed: Callable[[], object] | None = None) -> bool:
    """Whether the app ``node`` runs in is quitting to hand its terminal over.

    ``owed`` is the start that stands down when it is: the app runs it if the hand-over
    is called off, so what was skipped while asq waited is done then (round 24 of #257).

    Read with getattr: a view also runs under an app with no hand-over at all
    (``aisquare board -w``'s, a test's host).
    """
    app = node.app
    if getattr(app, "hand_off", None) is None:
        return False
    owe = getattr(app, "owe", None)
    if owed is not None and owe is not None:
        owe(node, owed)
    return True
