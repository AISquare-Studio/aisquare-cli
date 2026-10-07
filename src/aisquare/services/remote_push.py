"""Web Push: notifications on the phone when something needs the human (SPEC §5).

THIS IS THE SEAM, NOT THE FEATURE. The server builds its routes from
:func:`push_routes` and starts :func:`start_push_sender` in its lifespan; the
Remote-off and lockout paths call :func:`push_farewell` and
:func:`push_security_alert`. Every signature is final, so those callers are
written now; the bodies send nothing until the sender exists. The two module
functions must never block their caller, which is about to revoke the devices
it names — that holds trivially here and is the contract the sender keeps.

The sender, once it exists, lives at ``kit.lane_state["push"]``.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.routing import BaseRoute

    from aisquare.services.remote_server import RemoteKit


def push_routes(kit: RemoteKit) -> list[BaseRoute]:
    """``GET api/push``, subscribe, unsubscribe and test. None yet."""
    return []


def start_push_sender(kit: RemoteKit) -> Callable[[], None] | None:
    """Start the sender at ``kit.lane_state["push"]``; its stopper. Nothing to start yet."""
    return None


def push_farewell(device_ids: Collection[str], reason: str) -> None:
    """Tell these devices Remote is off, before they are revoked. Nothing is sent yet."""
    return None


def push_security_alert(device_ids: Collection[str], text: str) -> None:
    """Warn these devices that someone is guessing the password. Nothing is sent yet."""
    return None


__all__ = ["push_farewell", "push_routes", "push_security_alert", "start_push_sender"]
