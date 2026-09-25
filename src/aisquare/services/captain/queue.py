"""The attention queue's seam — the four calls the Actions server makes on it.

The queue itself (what needs the owner across every project, deduplicated,
ranked and resolved one by one) is its own card, T7
(tsk_01m3bq2pm4j35557z5aszt9pgg). Until it lands every call here says so, and
the ``attention``, ``next``, ``resolve`` and ``snooze`` tools turn that into a
refusal the owner hears — never an empty queue that reads as "nothing needs you".

T7 replaces these bodies and keeps the signatures: an item is a JSON-able dict
with at least ``id``, ``project``, ``agent``, ``kind``, ``text`` and ``status``.
"""

from __future__ import annotations


class QueueUnavailable(RuntimeError):
    """The attention queue is not built yet."""


_NOT_YET = (
    "the attention queue lands with T7 (tsk_01m3bq2pm4j35557z5aszt9pgg) and is not built "
    "yet — read a board with board(project) or since(project) instead"
)


def ranked(limit: int = 10) -> list[dict[str, object]]:
    """The open items, most urgent first, at most ``limit``."""
    raise QueueUnavailable(_NOT_YET)


def next_item() -> dict[str, object] | None:
    """The single most urgent open item, or ``None`` when nothing needs the owner."""
    raise QueueUnavailable(_NOT_YET)


def resolve(item_id: str, how: str) -> dict[str, object]:
    """Mark an item resolved, recording what was done about it."""
    raise QueueUnavailable(_NOT_YET)


def snooze(item_id: str, minutes: int) -> dict[str, object]:
    """Hide an item for ``minutes``; it comes back on its own."""
    raise QueueUnavailable(_NOT_YET)
