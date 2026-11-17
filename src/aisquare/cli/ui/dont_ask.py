"""The words of *Don't ask again* (R17), shared by the boxes that offer it and the reset.

No widget here: the confirmation box (``cli/ui/confirm_action.py``) and App settings' reset
(*Ask again before every action*, ``views/app_settings.py``) both say a kind by its verb, and
the box module imports App settings for its message, so these live apart from either.
"""

from __future__ import annotations

from collections.abc import Sequence

STOP = "stop"
PAUSE = "pause"
RESTART = "restart"

VERB: dict[str, str] = {STOP: "Stop", PAUSE: "Pause", RESTART: "Restart"}
"""Each kind's button, and the word its toasts and the reset's line name it by."""

ASK_AGAIN = "Ask again before every action"
"""The reset's button, under App settings ▸ User preferences ▸ Advanced."""
ASK_AGAIN_WHERE = f"⚙ Settings ▸ App settings ▸ User preferences ▸ Advanced ▸ {ASK_AGAIN}"
"""Where the reset is: the toast that says a box will not ask again names it, since nothing
else on screen will once the box has gone."""
EVERY_ACTION_ASKS = "Every action asks first"
"""The reset's line when no box was ticked: the button then has nothing to do."""


def verb_of(kind: str) -> str:
    """The word a kind is said by: its button's, or the kind capitalised, for one a later
    release added."""
    return VERB.get(kind, kind.capitalize())


def said_not_asking(kinds: Sequence[str]) -> str:
    """``Not asking before: Stop, Pause`` — the reset's line — or :data:`EVERY_ACTION_ASKS`."""
    if not kinds:
        return EVERY_ACTION_ASKS
    return "Not asking before: " + ", ".join(verb_of(kind) for kind in kinds)
