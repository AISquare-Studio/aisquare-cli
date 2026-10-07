"""Needs-you: what in the fleet is waiting on the human, across every project (SPEC §4).

THIS IS THE SEAM, NOT THE FEATURE. The data model and every signature below are
final, so the server (``remote_server.build_app``), the push sender
(``remote_push``) and the agent actions (``remote_actions``) are written and
type-checked against them before the scanner exists. The bodies answer as if
nothing had been scanned yet: no routes, no watcher, no frames, an empty feed.
``needs_agent_now`` raises ``NotImplementedError``, because an empty answer
there would read as "this agent shows no dialog" to a caller about to type into
it — the one guess that must never be made by default.

The watcher, once it exists, lives at ``kit.lane_state["needs"]``;
:func:`start_needs_watch` puts it there and every other seam reads it from
there. This module imports ``remote_server`` only for types: the server imports
this one inside functions, so neither is on the hook path (SPEC §0.2, §7.3).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from aisquare.models import FleetAgentStatus, ProjectInfo

if TYPE_CHECKING:
    from starlette.routing import BaseRoute

    from aisquare.services.remote_server import RemoteKit

NEEDS_KINDS = (
    "permission",
    "question",
    "plan",
    "board_question",
    "manager_down",
    "crashed",
    "limited",
    "lost",
    "fleet_down",
    "asked",
    "board_result",
    "interrupted",
)
"""Every kind of item, in rank order: the feed sorts by this, then by ``since``."""

DIALOG_SETTLE_SECONDS = 3.0
"""How long an action waits, after one Escape, for a dialog to close or a prompt to show."""


@dataclass(frozen=True)
class QuickAnswer:
    """One button a card offers: its label and the tmux key names it sends."""

    label: str
    keys: tuple[str, ...]


@dataclass(frozen=True)
class NeedsItem:
    """One thing that needs the human, with what they must read before answering it."""

    id: str
    kind: str
    project_id: str
    project_name: str
    agent: str | None
    agent_id: str | None
    reason: str
    excerpt: str
    detail: Mapping[str, object]
    answers: tuple[QuickAnswer, ...]
    since: datetime
    actions: tuple[str, ...]
    push_after: datetime | None
    """When a push may go out; ``None`` is feed only. Internal: never serialized."""

    def needs_item_json(self) -> dict[str, object]:
        """The wire shape (SPEC §1.4): the project as ``{id, name}``, ``push_after`` left out."""
        return {
            "id": self.id,
            "kind": self.kind,
            "project": {"id": self.project_id, "name": self.project_name},
            "agent": self.agent,
            "agent_id": self.agent_id,
            "reason": self.reason,
            "excerpt": self.excerpt,
            "detail": dict(self.detail),
            "answers": [{"label": a.label, "keys": list(a.keys)} for a in self.answers],
            "since": self.since.isoformat(timespec="seconds"),
            "actions": list(self.actions),
        }


@dataclass(frozen=True)
class AgentNow:
    """One agent, re-derived now — what an action or a quick answer checks before typing."""

    project: ProjectInfo
    status: FleetAgentStatus | None
    """``None``: the label's newest row has ended and has no window left."""
    tail: object | None
    """The transcript tail (``transcript.TranscriptTail`` once it exists)."""
    pane_is_agent: bool
    """The pane's foreground is the agent; ``False`` when it is not live."""
    pane_quiet: bool | None
    """``#{window_activity}`` older than ``fleet.ACTIVITY_WINDOW``; ``None``: tmux did not say."""
    items: tuple[NeedsItem, ...]
    """Every current item of this project whose ``agent`` is this label."""


def needs_agent_now(project: ProjectInfo, label: str, *, now: datetime | None = None) -> AgentNow:
    """One agent re-derived now, synchronously, never on the event loop.

    Not built yet. It raises rather than answering empty: an ``AgentNow`` with
    no items and no dialog is a claim about a live pane, and a caller trusting
    it would type into whatever that pane is showing.
    """
    raise NotImplementedError("needs-you is not built yet")


def needs_dialog_open(snap: AgentNow) -> bool:
    """Whether the agent shows a dialog an Enter would answer. Not built yet: ``False``."""
    return False


def needs_at_input_prompt(snap: AgentNow) -> bool:
    """Whether the agent waits at its input prompt. Not built yet: ``False``."""
    return False


def needs_item_current(snap: AgentNow, item_id: str) -> bool:
    """Whether ``item_id`` is still one of the agent's items. Not built yet: ``False``."""
    return False


# --- the seams remote_server calls --------------------------------------------------


def needs_routes(kit: RemoteKit) -> list[BaseRoute]:
    """``GET api/needs``, ``POST api/needs/dismiss``, ``POST api/needs/answer``. None yet."""
    return []


def start_needs_watch(kit: RemoteKit) -> Callable[[], None] | None:
    """Start the scanner at ``kit.lane_state["needs"]``; its stopper. Nothing to start yet."""
    return None


def needs_ws_frames(kit: RemoteKit) -> list[tuple[str, object]]:
    """``[("needs_you", {"items": [...]})]`` once the watcher has scanned; ``[]`` until then."""
    return []


def needs_scanned_iso(kit: RemoteKit) -> str | None:
    """When the watcher last scanned, for the heartbeat frame; ``None``: it never has."""
    return None


def needs_cli_payload() -> dict[str, object]:
    """``asq remote needs``: the feed, the same shape as ``GET api/needs``."""
    return {"items": [], "scanned_at": None}


__all__ = [
    "DIALOG_SETTLE_SECONDS",
    "NEEDS_KINDS",
    "AgentNow",
    "NeedsItem",
    "QuickAnswer",
    "needs_agent_now",
    "needs_at_input_prompt",
    "needs_cli_payload",
    "needs_dialog_open",
    "needs_item_current",
    "needs_routes",
    "needs_scanned_iso",
    "needs_ws_frames",
    "start_needs_watch",
]
