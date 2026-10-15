"""A project added on purpose, as a test's setup adds it (#139)."""

from __future__ import annotations

from pathlib import Path

from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.models import ProjectInfo


def add_project(root: Path) -> ProjectInfo:
    """``root``'s board project, added as ``team on`` or ``project onboard`` adds it.

    With ``[projects] capture`` off, the default, a hook or a board write in a directory
    nobody added records nothing, so a test of what they record works in an added one.
    """
    with store_session() as store:
        return store.onboard_project(team_project(root))
