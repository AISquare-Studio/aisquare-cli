"""Agent actions from the phone: tell, stop, restart, switch (SPEC §3).

THIS IS THE SEAM, NOT THE FEATURE. The write dispatcher already answers every
name in :data:`ACTION_ENDPOINTS` through :func:`action_handlers`, and builds
``GET api/actions/recent`` from :func:`action_routes`; both are empty until the
actions exist. :class:`ActionLedger` is the request ledger every write-gated
request passes (SPEC §1.5). This one keeps nothing: every request id begins,
none is ever replayed, so a retried request runs again exactly as it did before
there was a ledger.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from starlette.routing import BaseRoute

    from aisquare.services.remote_server import RemoteKit, WriteHandler

ACTION_ENDPOINTS: tuple[str, ...] = ()
"""The action names ``POST api/{name}`` accepts beside ``remote_server.WRITE_ENDPOINTS``."""


class LedgerEntry(TypedDict):
    """One finished request, as ``GET api/actions/recent`` and the ``action`` frame show it."""

    request_id: str
    endpoint: str
    status: int
    body: dict[str, object]
    at: str


class ActionLedger:
    """Per device: the finished requests and the ones still running, by ``request_id``."""

    def ledger_replay(
        self, device_id: str, request_id: str
    ) -> tuple[int, dict[str, object]] | None:
        """The stored ``(status, body)`` of a finished request; ``None``: not finished here."""
        return None

    def ledger_begin(self, device_id: str, request_id: str, endpoint: str) -> bool:
        """Mark a request as running; ``False`` while one with that id still is."""
        return True

    def ledger_finish(
        self, device_id: str, request_id: str, status: int, body: dict[str, object]
    ) -> None:
        """Store how a request ended, refusals included, so a retry gets the same answer."""
        return None

    def ledger_recent(self, device_id: str) -> list[LedgerEntry]:
        """This device's finished requests, newest first."""
        return []


def new_action_ledger() -> ActionLedger:
    """The ledger a new app starts with."""
    return ActionLedger()


def action_handlers() -> dict[str, WriteHandler]:
    """The write handlers behind :data:`ACTION_ENDPOINTS`. None yet."""
    return {}


def action_routes(kit: RemoteKit) -> list[BaseRoute]:
    """``GET api/actions/recent``. None yet."""
    return []


__all__ = [
    "ACTION_ENDPOINTS",
    "ActionLedger",
    "LedgerEntry",
    "action_handlers",
    "action_routes",
    "new_action_ledger",
]
