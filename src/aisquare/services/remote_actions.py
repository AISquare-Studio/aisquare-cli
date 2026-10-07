"""Agent actions from the phone (SPEC §3), and the request ledger every write passes (§1.5).

The actions themselves (tell, stop, restart, switch) are not built yet: the write
dispatcher answers every name in :data:`ACTION_ENDPOINTS` through
:func:`action_handlers`, and both are empty.

:class:`ActionLedger` keeps, per device, how its recent write-gated requests
ended. A retried ``request_id`` gets the first answer instead of a second run: a
phone that slept through a 30 s restart retries it, and must not start a second
hand-over. ``GET api/actions/recent`` (:func:`action_routes`) and the stream's
``action`` frame show the ledger to the device that made the requests, and to
no other.

``remote_server`` imports this module inside functions only, so neither is on
the hook path (SPEC §0.2, §7.3).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, TypedDict, TypeVar

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import BaseRoute

    from aisquare.services.remote_server import Device, RemoteKit, WriteHandler

ACTION_ENDPOINTS: tuple[str, ...] = ()
"""The action names ``POST api/{name}`` accepts beside ``remote_server.WRITE_ENDPOINTS``."""

ACTION_LEDGER_SIZE = 50
"""Finished requests the ledger keeps per device; the oldest goes first."""

ACTION_LEDGER_TTL = timedelta(minutes=15)
"""How long a finished request can be replayed or shown. Far longer than a phone sleeps
through one restart, and short enough that a request id the page reuses much later
runs anew rather than getting a stale answer back."""


class LedgerEntry(TypedDict):
    """One finished request, as ``GET api/actions/recent`` and the ``action`` frame show it."""

    request_id: str
    endpoint: str
    status: int
    body: dict[str, object]
    at: str


_Record = TypeVar("_Record")


def _ledger_now() -> datetime:
    return datetime.now(UTC)


def _ledger_drop_expired(
    book: dict[str, dict[str, tuple[_Record, datetime]]], now: datetime
) -> None:
    """Forget every record of ``book`` (device → request id → (record, when)) past the TTL."""
    for device_id in list(book):
        kept = {
            request_id: held
            for request_id, held in book[device_id].items()
            if now - held[1] < ACTION_LEDGER_TTL
        }
        if kept:
            book[device_id] = kept
        else:
            del book[device_id]


class ActionLedger:
    """Per device: how its recent write-gated requests ended, and which are still running.

    In memory only, one per app. A server that restarts forgets it, and a retry
    then runs again, as every retry did before there was a ledger. Per device it
    keeps at most :data:`ACTION_LEDGER_SIZE` finished requests younger than
    :data:`ACTION_LEDGER_TTL`, and the ids still running. A running id is
    forgotten after the TTL too, so a request whose ending was never recorded
    cannot answer ``in_progress`` for the life of the server. Every pass drops
    what expired for EVERY device: a phone that never comes back must not keep
    its last answers in memory until the server stops.

    The server calls it from the event loop (the dispatcher, ``kit_route``, each
    socket's tick) and tests call it from their own threads, so one lock guards
    each method.
    """

    def __init__(self, *, clock: Callable[[], datetime] = _ledger_now) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._finished: dict[str, dict[str, tuple[LedgerEntry, datetime]]] = {}
        """device id → request id → (its entry, when it ended); oldest first."""
        self._running: dict[str, dict[str, tuple[str, datetime]]] = {}
        """device id → request id → (its endpoint, when it began)."""

    def _ledger_forget_expired(self) -> datetime:
        now = self._clock()
        _ledger_drop_expired(self._finished, now)
        _ledger_drop_expired(self._running, now)
        return now

    def ledger_replay(
        self, device_id: str, request_id: str
    ) -> tuple[int, dict[str, object]] | None:
        """The stored ``(status, body)`` of a finished request; ``None``: not finished here."""
        with self._lock:
            self._ledger_forget_expired()
            held = self._finished.get(device_id, {}).get(request_id)
        return None if held is None else (held[0]["status"], held[0]["body"])

    def ledger_begin(self, device_id: str, request_id: str, endpoint: str) -> bool:
        """Mark a request as running; ``False`` while one with that id still is."""
        with self._lock:
            now = self._ledger_forget_expired()
            running = self._running.setdefault(device_id, {})
            if request_id in running:
                return False
            running[request_id] = (endpoint, now)
            return True

    def ledger_finish(
        self, device_id: str, request_id: str, status: int, body: dict[str, object]
    ) -> None:
        """Store how a request ended, refusals included, so a retry gets the same answer."""
        with self._lock:
            now = self._ledger_forget_expired()
            running = self._running.get(device_id, {})
            began = running.pop(request_id, None)
            if not running:
                self._running.pop(device_id, None)
            entry: LedgerEntry = {
                "request_id": request_id,
                "endpoint": "" if began is None else began[0],
                "status": status,
                "body": body,
                "at": now.isoformat(timespec="seconds"),
            }
            finished = self._finished.setdefault(device_id, {})
            finished.pop(request_id, None)  # a repeat ends up newest, not where it first was
            finished[request_id] = (entry, now)
            while len(finished) > ACTION_LEDGER_SIZE:
                del finished[next(iter(finished))]

    def ledger_recent(self, device_id: str) -> list[LedgerEntry]:
        """This device's finished requests, newest first."""
        with self._lock:
            self._ledger_forget_expired()
            held = self._finished.get(device_id, {})
            return [entry.copy() for entry, _ended in reversed(held.values())]


def new_action_ledger() -> ActionLedger:
    """The ledger a new app starts with."""
    return ActionLedger()


def action_handlers() -> dict[str, WriteHandler]:
    """The write handlers behind :data:`ACTION_ENDPOINTS`. None yet."""
    return {}


def action_routes(kit: RemoteKit) -> list[BaseRoute]:
    """``GET api/actions/recent``: this device's finished requests, newest first.

    What a phone that slept through a long action reads when it wakes, to learn
    how the action ended without sending it again. Only the asking device's own
    requests: what another phone asked for is not this one's business.
    """
    from starlette.responses import JSONResponse

    async def ledger_recent_endpoint(
        request: Request, device: Device, body: dict[str, Any]
    ) -> Response:
        return JSONResponse({"actions": kit.ledger.ledger_recent(device.id)})

    return [
        kit.kit_route(
            "/api/actions/recent", ledger_recent_endpoint, methods=["GET"], write_gated=False
        )
    ]


__all__ = [
    "ACTION_ENDPOINTS",
    "ACTION_LEDGER_SIZE",
    "ACTION_LEDGER_TTL",
    "ActionLedger",
    "LedgerEntry",
    "action_handlers",
    "action_routes",
    "new_action_ledger",
]
