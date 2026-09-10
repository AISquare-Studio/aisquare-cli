"""``GET /v1/me``: which run this developer may ask against.

The descriptor says *how* to deliver for a run. It never said *which* run — that
was ``AISQUARE_CI_RUN``, an environment variable a controller handed out per
cohort. Fine for a harness, useless for a person, and the gap that stopped
``aisquare login`` from being enough to join an experiment. This module closes
it: one call at ``SessionStart``, cached, that says who the server resolved the
bearer to and which run is published in each workspace they belong to.

**It is bounded and negatively cached, for the reason the descriptor is.** This
sits in front of the descriptor fetch on the synchronous session-start path, so
a hanging ``/v1/me`` would cost every session its whole ceiling and then the
descriptor's on top. It therefore borrows
:data:`~aisquare.services.ci_descriptor.DESCRIPTOR_DEADLINE_MS` and caches a
refusal for :data:`~aisquare.services.ci_descriptor.REFUSAL_TTL_SECONDS`, so a
server that is down costs one probe per minute rather than one per prompt. The
plan this implements did not say that; the review of the branch it was written
against is why it does.

**The run the client uses, in order.** ``AISQUARE_CI_RUN`` when set — the
harness, the joint smoke and every ``CITEST_*`` identity depend on being able to
say "this run" from one shell variable, and it keeps precedence. Otherwise the
``active_run_id`` of the workspace this project is bound to
(``[experiment].workspace``). Otherwise, when the developer belongs to exactly
one workspace, that one. Otherwise nothing, and the turn records ``no_run`` with
a reason ``doctor`` can print — guessing between several workspaces would bind a
project to whichever the server happened to list first.

**Cached per bearer, not per user.** The cache file is keyed by a hash of the
token, so signing out and in as somebody else cannot serve the previous
identity's routing, and a re-issued token starts cold. The hash is truncated
because a filename is not a secret store; the token itself is never written.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from aisquare.core import paths
from aisquare.models import ClientReason
from aisquare.services import ci_client
from aisquare.services.ci_contract import MeDocument, WorkspaceMembership, clip, first_error
from aisquare.services.ci_descriptor import (
    DESCRIPTOR_DEADLINE_MS,
    REFUSAL_TTL_SECONDS,
)

ME_PATH = "/v1/me"
"""Server-relative; joined to the configured base URL."""

MAX_ME_BYTES = 65_536
"""A principal and a handful of workspaces. Past this it is not a ``me.v1``."""

CACHE_TTL_SECONDS = 300
"""How long an answer is reused.

Shorter than a token's life and longer than a session start, which is the window
that matters: memberships change on human timescales, and a developer added to a
workspace should not have to restart their editor for more than a few minutes.
The descriptor's own expiry is unrelated and is not borrowed — that one is the
server's statement about a run, this one is ours about an identity."""


@dataclass(frozen=True)
class MeResult:
    """The document, or the reason there is none."""

    me: MeDocument | None
    detail: str = ""
    from_cache: bool = False
    status: int | None = None
    """The HTTP status of a refusal, when the server answered with one. ``doctor``
    picks its fix from this rather than from words in ``detail``."""

    @property
    def reason(self) -> ClientReason:
        return ClientReason.none if self.me is not None else ClientReason.descriptor_unavailable


def current(*, base: str, key: str, now: datetime | None = None) -> MeResult:
    """The live ``me.v1`` for this bearer: the cache when fresh, else a fetch.

    Never raises. ``base`` is the validated endpoint; ``key`` the bearer (sent,
    never stored).
    """
    moment = now or datetime.now(tz=UTC)
    cached = _read_cache(key, moment)
    if cached is not None:
        return MeResult(cached, "cached", from_cache=True)
    refused = _read_refusal(key, moment, base)
    if refused is not None:
        return MeResult(None, f"{refused} (refusal cached)", from_cache=True)
    result = fetch(base=base, key=key)
    if result.me is None:
        _write_refusal(key, result.detail, moment, base)
    return result


def fetch(
    *, base: str, key: str, cache: bool = True, deadline_ms: int = DESCRIPTOR_DEADLINE_MS
) -> MeResult:
    """One GET, one attempt, every failure its own detail. Never raises.

    ``cache=False`` answers without leaving a file behind — ``doctor`` uses it,
    because a diagnostic must not create state. A document that arrives still
    CLEARS a cached refusal either way: the refusal is a claim about the server
    that this answer has just disproved, and leaving it would make ``doctor``
    print a healthy identity while every hook kept reading the stale negative.
    ``deadline_ms`` exists for ``doctor`` too, which bounds its probes tighter
    than the session-start path does.
    """
    result = ci_client.exchange(
        f"{base}{ME_PATH}",
        method="GET",
        deadline_ms=deadline_ms,
        headers=ci_client.headers_for(key, json_body=False),
        max_body=MAX_ME_BYTES,
    )
    if result.reason is not None:
        return MeResult(None, f"{result.reason.value}: {result.detail}")
    if result.status != 200:
        return MeResult(None, _status_detail(result.status), status=result.status)
    me, detail = parse_me(result.body)
    if me is None:
        return MeResult(None, detail)
    if cache:
        _write_cache(key, result.body)
    else:
        _clear_refusal(key)
    return MeResult(me, "fetched")


def parse_me(body: str) -> tuple[MeDocument | None, str]:
    """Turn a body into a ``me.v1``, or say exactly why not. Never raises."""
    try:
        raw = json.loads(body)
    except Exception:
        return None, "me is not JSON"
    if not isinstance(raw, dict):
        return None, f"me is {type(raw).__name__}"
    version = raw.get("contract_version")
    if type(version) is not int or version != 1:
        return None, f"me speaks contract_version {clip(repr(version), 40)}, this build speaks 1"
    try:
        return MeDocument.model_validate(raw), "ok"
    except ValidationError as exc:
        return None, f"me: {first_error(exc)}"


def run_for(me: MeDocument, workspace_id: str | None) -> tuple[str | None, str]:
    """The run this client should use, and why — never raises.

    Returns ``(run_id, detail)``. ``run_id`` is ``None`` whenever the developer
    has nowhere to ask, which is a normal state and not a failure: a token can
    be perfectly good while no run is published for them yet.
    """
    if not me.workspaces:
        return None, "signed in, but a member of no workspace"
    member = me.membership(workspace_id)
    if member is None:
        if workspace_id:
            return None, f"not a member of {workspace_id}"
        listed = ", ".join(m.workspace_id for m in me.workspaces)
        return None, (
            f"a member of {len(me.workspaces)} workspaces ({listed}) and none is bound — "
            "set experiment.workspace"
        )
    if member.active_run_id is None:
        return None, f"no run published in {member.workspace_id}"
    return member.active_run_id, f"run {member.active_run_id} from {member.workspace_id}"


def _status_detail(status: int | None) -> str:
    if status == 401:
        return "token rejected (401)"
    if status == 403:
        return "token not allowed to read its own identity (403)"
    return f"http {status}"


def _key_digest(key: str) -> str:
    """A short, stable, one-way name for a bearer.

    Keyed on the token so signing in as somebody else cannot serve the previous
    identity's routing, and a re-issued token starts cold. Truncated because a
    filename is a cache key and not a secret store; the token is never written.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _cache_path(key: str) -> Path:
    return paths.ci_me_path(_key_digest(key))


def _refusal_path(key: str) -> Path:
    return _cache_path(key).with_suffix(".refused.json")


def _read_cache(key: str, now: datetime) -> MeDocument | None:
    """A fresh cached document for this bearer, or ``None`` for any other state."""
    try:
        raw = json.loads(_cache_path(key).read_text(encoding="utf-8"))
        until = datetime.fromisoformat(raw["until"])
        body = raw["body"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(body, str) or now >= until:
        return None
    me, _ = parse_me(body)
    return me


def _write_cache(key: str, body: str) -> None:
    """Store the answer with its own expiry. Never raises."""
    until = (datetime.now(tz=UTC) + timedelta(seconds=CACHE_TTL_SECONDS)).isoformat()
    _replace(_cache_path(key), json.dumps({"body": body, "until": until}))
    _clear_refusal(key)


def _read_refusal(key: str, now: datetime, base: str) -> str | None:
    """The detail of a recent refusal against ``base``, or ``None``."""
    try:
        raw = json.loads(_refusal_path(key).read_text(encoding="utf-8"))
        until = datetime.fromisoformat(raw["until"])
        detail = raw["detail"]
        scope = raw.get("endpoint")
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(detail, str) or now >= until:
        return None
    if scope != base.rstrip("/"):
        # A refusal from one server must not answer for the next: repointing
        # AISQUARE_CI_URL invalidates it, the same rule the descriptor's
        # refusal cache follows.
        return None
    return detail


def _write_refusal(key: str, detail: str, now: datetime, base: str) -> None:
    until = (now + timedelta(seconds=REFUSAL_TTL_SECONDS)).isoformat()
    _replace(
        _refusal_path(key),
        json.dumps({"detail": detail, "until": until, "endpoint": base.rstrip("/")}),
    )


def _clear_refusal(key: str) -> None:
    with contextlib.suppress(OSError):
        _refusal_path(key).unlink()


def forget(key: str) -> None:
    """Drop this bearer's cached answer and any cached refusal. Never raises."""
    for path in (_cache_path(key), _refusal_path(key)):
        with contextlib.suppress(OSError):
            path.unlink()


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


__all__ = [
    "CACHE_TTL_SECONDS",
    "MAX_ME_BYTES",
    "ME_PATH",
    "MeResult",
    "WorkspaceMembership",
    "current",
    "fetch",
    "forget",
    "parse_me",
    "run_for",
]
