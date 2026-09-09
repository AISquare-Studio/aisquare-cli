"""The device-grant wait, for a caller that is not a terminal.

``cli/auth.py`` owns the terminal sign-in: it prints the code, opens the
browser, runs a live countdown, watches for Esc and polls. The fleet UI's
Accounts page needs the same wait with none of the printing — its worker
thread has a cancel flag and a widget to update, nothing more — so the poll
loop lives here once more in the shape RFC 8628 §3.4 describes, with the
clock, the sleep and the cancel check as parameters. It reaches the provider
only through ``services.iam``, exactly as the terminal does, and it raises the
same ``IamError`` codes the terminal maps to exit statuses (``cancelled``,
``expired``, ``access_denied``, ``paused``, ``unsupported_server``).
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any

from aisquare.services import iam

MAX_BACKOFF_SECONDS = 60.0
"""Longest wait between polls after the token endpoint could not be reached."""
MAX_RETRY_AFTER_SECONDS = 30
"""Cap on a 429's ``Retry-After``, as the terminal caps it."""
GRACE_SECONDS = 60
"""How long past ``expires_in`` the loop keeps asking, so a late approval still lands."""
CANCEL_POLL_SECONDS = 0.5
"""How often a sleeping wait re-checks the cancel flag."""


def wait_for_token(
    endpoints: iam.Endpoints,
    grant: iam.DeviceAuthorization,
    *,
    cancelled: Callable[[], bool],
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    jitter: Callable[[float, float], float] = random.uniform,
    poll: Callable[[iam.Endpoints, str], iam.PollOutcome] = iam.poll_token,
) -> dict[str, Any]:
    """Poll until the browser approves the grant; the token response comes back.

    Interval + 20 % + jitter between polls; ``slow_down`` widens the interval,
    a 429 waits its ``Retry-After`` (capped), an unreachable endpoint backs off
    exponentially, and a local deadline of ``expires_in`` plus a grace period
    ends it. ``cancelled`` is consulted every half second of every wait, so a
    Cancel button answers promptly — and once more when a token has arrived,
    so a cancel that lands during the poll is still a cancel.
    """
    deadline = monotonic() + grant.expires_in + GRACE_SECONDS
    interval = float(max(1, grant.interval))
    backoff: float | None = None
    while True:
        wait = backoff if backoff is not None else interval * 1.2 + jitter(0, 1)
        if not _pause(wait, cancelled, sleep, monotonic):
            raise iam.IamError("cancelled", "Sign-in cancelled. Nothing was stored.")
        if monotonic() > deadline:
            raise iam.IamError("expired", "The code expired before it was approved.")
        try:
            outcome = poll(endpoints, grant.device_code)
        except iam.IamError as exc:
            if exc.code != "unreachable":
                raise
            backoff = min(MAX_BACKOFF_SECONDS, (backoff or interval) * 2)
            continue
        backoff = None
        if outcome.kind == "token" and outcome.token is not None:
            # Cancel pressed while the poll was in flight: the server said yes,
            # the user said no, and the user wins — a token that is never stored
            # simply expires on its own.
            if cancelled():
                raise iam.IamError("cancelled", "Sign-in cancelled. Nothing was stored.")
            return dict(outcome.token)
        if outcome.kind == "pending":
            continue
        if outcome.kind == "slow_down":
            interval = float(outcome.interval or interval + 5)
            continue
        if outcome.kind == "rate_limited":
            backoff = float(min(outcome.retry_after or 30, MAX_RETRY_AFTER_SECONDS))
            continue
        if outcome.kind == "denied":
            raise iam.IamError("access_denied", "The request was denied in the browser.")
        if outcome.kind == "expired":
            raise iam.IamError("expired", "The code expired before it was approved.")
        if outcome.kind == "paused":
            raise iam.IamError("paused", "CLI sign-in is temporarily paused. Try again later.")
        raise iam.IamError(
            "unsupported_server", f"Unexpected answer from {endpoints.issuer} while waiting."
        )


def _pause(
    seconds: float,
    cancelled: Callable[[], bool],
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> bool:
    """Sleep ``seconds`` in short steps; False the moment ``cancelled`` says so."""
    until = monotonic() + seconds
    while True:
        if cancelled():
            return False
        remaining = until - monotonic()
        if remaining <= 0:
            return True
        sleep(min(CANCEL_POLL_SECONDS, remaining))
