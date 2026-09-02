"""The delivery descriptor: the only run document the client ever fetches.

``GET /v1/experiment/runs/{run_id}`` returns ``client-delivery-descriptor.v1``,
and everything the CLI does for a run is driven by it — which hooks call the
server, where, under what ceiling, and whether the recall tool is exposed. It
carries no architecture, source, reader or arm field, so the client is
structurally unable to know which arm it is running: that is the blinding
mechanism, and this module must never be given a second source of any of those
facts.

Fetched once per session and cached to disk until ``expires_at``, because the
hooks are separate short-lived processes and a fetch in front of every prompt
would put a second round trip on the path the experiment measures. The cache
holds descriptors and nothing else.

Every refusal is its own detail — token rejected, run unknown, expired,
contract skew, unreadable — under one reason, ``descriptor_unavailable``,
because from the turn's point of view they are the same fact: the client could
not learn how to deliver, so it did not. Nothing here raises.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import ValidationError

from aisquare.core import paths
from aisquare.models import ClientReason
from aisquare.services import ci_client
from aisquare.services.ci_contract import (
    CONTRACT_VERSION,
    DeliveryDescriptor,
    clip,
    first_error,
    parse_error,
)

DESCRIPTOR_PATH = "/v1/experiment/runs/"
"""Server-relative; joined to the configured base URL."""

DESCRIPTOR_DEADLINE_MS = 10_000
"""The fetch's own wall-clock ceiling. The hook ceiling comes *from* the
descriptor, so this one cannot; a bounded constant is the honest choice, and
it sits well under any hook timeout an agent applies."""

MAX_DESCRIPTOR_BYTES = 65_536


@dataclass(frozen=True)
class DescriptorResult:
    """A descriptor, or the reason there is none."""

    descriptor: DeliveryDescriptor | None
    detail: str = ""
    from_cache: bool = False
    status: int | None = None
    """The HTTP status of a refusal, when the server answered with one. ``doctor``
    chooses its fix from this rather than from words in ``detail``, which a
    server message could contain by accident."""

    @property
    def reason(self) -> ClientReason:
        return (
            ClientReason.none
            if self.descriptor is not None
            else ClientReason.descriptor_unavailable
        )


def current(run: str, *, base: str, key: str, now: datetime | None = None) -> DescriptorResult:
    """The live descriptor for ``run``: the cache when it is fresh, else a fetch.

    Never raises. ``run`` is the validated ``run_…`` id; ``base`` the validated
    endpoint; ``key`` the bearer token (sent, never stored).
    """
    cached = _read_cache(run, now or datetime.now(tz=UTC))
    if cached is not None:
        return DescriptorResult(cached, "cached", from_cache=True)
    return fetch(run, base=base, key=key, now=now)


def fetch(
    run: str,
    *,
    base: str,
    key: str,
    cache: bool = True,
    now: datetime | None = None,
    deadline_ms: int = DESCRIPTOR_DEADLINE_MS,
) -> DescriptorResult:
    """One GET, one attempt, every failure a distinct detail. Never raises.

    ``cache=False`` answers the question without leaving a file behind —
    ``doctor`` uses it, because a diagnostic must not create state.
    """
    result = ci_client.exchange(
        f"{base}{DESCRIPTOR_PATH}{run}",
        method="GET",
        deadline_ms=deadline_ms,
        headers=ci_client.headers_for(key, json_body=False),
        max_body=MAX_DESCRIPTOR_BYTES,
    )
    if result.reason is not None:
        return DescriptorResult(None, f"{result.reason.value}: {result.detail}")
    if result.status != 200:
        return DescriptorResult(
            None, _status_detail(result.status, result.body), status=result.status
        )
    descriptor, detail = parse_descriptor(result.body, now=now)
    if descriptor is None:
        return DescriptorResult(None, detail)
    if descriptor.run_id != run:
        return DescriptorResult(None, f"descriptor names {descriptor.run_id}, not {run}")
    if cache:
        _write_cache(run, result.body)
    return DescriptorResult(descriptor, "fetched")


def parse_descriptor(
    body: str, *, now: datetime | None = None
) -> tuple[DeliveryDescriptor | None, str]:
    """Turn a body into a descriptor, or say exactly why not. Never raises.

    The contract version is checked before the shape, like the hook ladder,
    so a server that moved on reports "skew" rather than "schema".
    """
    try:
        raw = json.loads(body)
    except Exception:
        return None, "descriptor is not JSON"
    if not isinstance(raw, dict):
        return None, f"descriptor is {type(raw).__name__}"
    version = raw.get("contract_version")
    if type(version) is not int or version != CONTRACT_VERSION:
        return None, (
            f"descriptor speaks contract_version {clip(repr(version), 40)}, "
            f"this build speaks {CONTRACT_VERSION}"
        )
    try:
        descriptor = DeliveryDescriptor.model_validate(raw)
    except ValidationError as exc:
        return None, f"descriptor: {first_error(exc)}"
    moment = now or datetime.now(tz=UTC)
    if descriptor.expired(moment):
        return None, (
            f"descriptor expired at {descriptor.expires_at} "
            f"(client clock {moment:%Y-%m-%dT%H:%M:%SZ})"
        )
    return descriptor, "ok"


def _status_detail(status: int | None, body: str = "") -> str:
    """One line for a refusal: the CLI's reading of the status, then the server's
    own ``error.v1`` code and sentence when the body carried one."""
    if status == 401:
        detail = "token rejected (401)"
    elif status == 403:
        detail = "token not allowed to read this run (403)"
    elif status == 404:
        detail = "run not found (404)"
    else:
        detail = f"http {status}"
    error = parse_error(body)
    if error is None:
        return detail
    return clip(f"{detail} — {error.code}: {error.message}")


def _read_cache(run: str, now: datetime) -> DeliveryDescriptor | None:
    """A fresh cached descriptor for ``run``, or ``None`` for any other state."""
    try:
        body = paths.ci_descriptor_path(run).read_text(encoding="utf-8")
    except OSError:
        return None
    descriptor, _ = parse_descriptor(body, now=now)
    if descriptor is None or descriptor.run_id != run:
        return None
    return descriptor


def _write_cache(run: str, body: str) -> None:
    """Replace the cached descriptor in one step. Never raises."""
    target = paths.ci_descriptor_path(run)
    temporary = target.with_suffix(f".{os.getpid()}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(body, encoding="utf-8")
        os.replace(temporary, target)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()


def forget(run: str) -> None:
    """Drop the cached descriptor for ``run``. Never raises."""
    with contextlib.suppress(OSError):
        paths.ci_descriptor_path(run).unlink()
