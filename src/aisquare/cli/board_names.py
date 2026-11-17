"""What the HUMAN board calls each session (#140): the name asq's left pane gives its agent.

The owner, verbatim: "the board currently has persona.<id> sort of format which is impossible
to understand, can that be just the name shown on left pane". asq's board, ``board -w`` and
``aisquare board``'s text name a session by ``services.team.board_name`` (its fleet row's label,
live or ended, as a relabel left it, else its own, else its role), and tell two sessions of one
name apart
here, by the time each started, as the board shows its other times (``cli.common.local_time``).
The agent-facing text keeps ids (``services.team.authors_of``, ``event_line`` without names).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

from aisquare.cli.common import local_time
from aisquare.core.store import store_session
from aisquare.models import TeamSession
from aisquare.services import team as team_service

_log = logging.getLogger(__name__)

BOARD_NAME_CELLS = 24
"""The most a board VIEW gives a name (asq's board, ``board -w``): a longer label ends in ``…``,
so a feed line or a sessions row keeps its chip, as ``role·<id>`` (at most 17) did."""


def _fit(name: str, cells: int | None) -> str:
    return name if cells is None or len(name) <= cells else name[: max(cells - 1, 1)] + "…"


def board_names(
    sessions: Sequence[TeamSession],
    fleet_labels: Mapping[str, str] | None = None,
    *,
    cells: int | None = None,
) -> dict[str, str]:
    """Session id → ``team.board_name``, what the human board calls each session, in place of
    ``role·<id>``. Two sessions of one name (a restarted agent, an earlier round's agent of the
    same label) are told apart only then: the live one, else the newest, keeps the name, and
    each other gets the time it started, ``coder-1 (14:02)``, or its short id where that time
    is shared too. ``cells`` caps each (:data:`BOARD_NAME_CELLS` for a view), the name shortened
    and never what tells it apart."""
    names = {session.id: team_service.board_name(session, fleet_labels) for session in sessions}
    told_apart: set[str] = set()
    groups: dict[str, list[TeamSession]] = {}
    for session in sessions:
        groups.setdefault(names[session.id], []).append(session)
    for name, group in groups.items():
        if len(group) < 2:
            continue
        kept, *others = sorted(
            group, key=lambda s: (s.ended_at is None, s.started_at), reverse=True
        )
        stamps = {s.id: f"{local_time(s.started_at):%H:%M}" for s in others}
        for session in others:
            stamp = stamps[session.id]
            shared = sum(1 for other in stamps.values() if other == stamp) > 1
            suffix = f" ({team_service.short_id(session.id) if shared else stamp})"
            room = None if cells is None else cells - len(suffix)
            names[session.id] = _fit(name, room) + suffix
            told_apart.add(session.id)
        names[kept.id] = name
    return {sid: name if sid in told_apart else _fit(name, cells) for sid, name in names.items()}


def read_board_names(sessions: Sequence[TeamSession]) -> dict[str, str]:
    """:func:`board_names`, with ``team.fleet_labels`` read in a store session of its own: for a
    board painted from ``team.board_data`` (``aisquare board``), whose session has closed. A
    read that fails costs the fleet rows' names, never the board."""
    try:
        with store_session() as store:
            fleet_labels = team_service.fleet_labels(store, sessions)
    except Exception as exc:
        _log.warning("the board's names read the sessions' own labels (%s)", exc)
        fleet_labels = {}
    return board_names(sessions, fleet_labels)
