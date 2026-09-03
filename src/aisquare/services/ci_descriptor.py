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
from datetime import UTC, datetime, timedelta
from pathlib import Path

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

REFUSAL_TTL_SECONDS = 60
"""How long a failed fetch is remembered before the next hook tries again.

Only successes were cached, so every failure state re-ran the full ``GET`` in
front of every prompt, bounded by :data:`DESCRIPTOR_DEADLINE_MS` rather than
the run's ceiling — ten seconds per prompt against an endpoint that accepts
TCP and never answers, forever. A refusal is now cached for a short window: the
first prompt pays the probe, the next ones record ``descriptor_unavailable``
at once, and the window is short enough that a fixed server is noticed."""


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
    moment = now or datetime.now(tz=UTC)
    cached = _read_cache(run, moment)
    if cached is not None:
        return DescriptorResult(cached, "cached", from_cache=True)
    refused = _read_refusal(run, moment)
    if refused is not None:
        return DescriptorResult(None, f"{refused} (refusal cached)", from_cache=True)
    result = fetch(run, base=base, key=key, now=now)
    if result.descriptor is None:
        _write_refusal(run, result.detail, moment)
    return result


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
    _replace(paths.ci_descriptor_path(run), body)
    with contextlib.suppress(OSError):
        _refusal_path(run).unlink()  # a descriptor arrived; nothing is refused now


def _refusal_path(run: str) -> Path:
    return paths.ci_descriptor_path(run).with_suffix(".refused.json")


def _read_refusal(run: str, now: datetime) -> str | None:
    """The detail of a recent refusal, or ``None`` when none is fresh."""
    try:
        raw = json.loads(_refusal_path(run).read_text(encoding="utf-8"))
        until = datetime.fromisoformat(raw["until"])
        detail = raw["detail"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(detail, str) or now >= until:
        return None
    return detail


def _write_refusal(run: str, detail: str, now: datetime) -> None:
    until = (now + timedelta(seconds=REFUSAL_TTL_SECONDS)).isoformat()
    _replace(_refusal_path(run), json.dumps({"detail": detail, "until": until}))


def _replace(target: Path, body: str) -> None:
    """Write ``body`` to ``target`` in one step. Never raises."""
    temporary = target.with_suffix(f".{os.getpid()}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(body, encoding="utf-8")
        os.replace(temporary, target)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()


def forget(run: str) -> None:
    """Drop the cached descriptor and any cached refusal for ``run``. Never raises."""
    for path in (paths.ci_descriptor_path(run), _refusal_path(run)):
        with contextlib.suppress(OSError):
            path.unlink()
