"""Opt-in anonymous usage sharing: the batch, the shipment, the switch and the consent.

What is shared, and when, is ``docs/usage-telemetry.md`` (the wire contract and, in plain words,
what is and is never collected). This module is its client:

- :func:`build_batch` turns the local daily rollups (``services.usage_stats.daily_rollups``)
  into the contract's body: today, and every day of the last 31 not yet acknowledged, oldest
  first. Repositories are sent as ``HMAC-SHA256(per-install salt, remote or root)[:16]`` with
  four coarse traits and nothing else; commands and pages as counts of their NAMES. The one
  function builds what :func:`preview` shows and what :func:`ship` posts, so they cannot differ.
- :func:`ship` posts it, off every hot path (``aisquare metrics ship`` in a detached process, or
  a TUI worker): 5 s timeouts, the AISquare sign-in as a bearer when there is one and a retry
  once without it on a 401, a backoff on any failure, the days acknowledged only when the server
  says 200. A failed shipment loses nothing: the days are rebuilt from the local record and sent
  again, and the server upserts by (install, day).
- :func:`enable` and :func:`disable` flip the switch. Off purges what was kept of the shipments
  and sends the one ``opt_out`` ping, best effort; nothing is sent after it, and nothing at all
  while sharing is off or forced off (``core.sharing.forced_off_reason``).

The transport is a seam (:data:`Transport`): the tests give :func:`ship` a fake, and no test
reaches a server. The endpoint (``/api/v1/cli/metrics/`` of ``AISquare-Studio-BE``) is the
contract's.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import platform
import re
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any

from aisquare.core import locking, sharing
from aisquare.core.sharing import SharingState
from aisquare.core.store import ContextStore
from aisquare.core.version import __version__
from aisquare.models import DailyRollup, SharingStatus, ShipReport
from aisquare.services import iam, install_route, usage_stats
from aisquare.services import settings as settings_service

SCHEMA_VERSION = 1
ENDPOINT_PATH = "/api/v1/cli/metrics/"
WINDOW_DAYS = 31
"""A batch holds at most this many days: today and the 30 before."""
MAX_FEATURES = 200
MAX_REPOS = 25
MAX_BODY_BYTES = 250_000
"""The contract caps a body at 256 KB; a batch past this drops its oldest days (they go next)."""
TIMEOUT_S = 5.0
PLACEHOLDER_ID = "00000000-0000-4000-8000-000000000000"

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._:-]")

Transport = Callable[[str, dict[str, Any], str | None, float], iam.HttpResult]
"""``(url, body, bearer token or None, timeout) -> result``: how a body reaches the server."""


def default_transport(
    url: str, body: dict[str, Any], token: str | None, timeout: float
) -> iam.HttpResult:
    return iam.post_json(url, body, token=token, timeout=timeout)


class SharingForcedOff(Exception):
    """Sharing cannot be turned on: ``DO_NOT_TRACK``, ``AISQUARE_NO_TELEMETRY`` or ``CI``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --- the batch ----------


@dataclass(frozen=True)
class Batch:
    """The body a shipment posts, and the digest of each day in it (what is acknowledged)."""

    body: dict[str, Any]
    digests: dict[str, str]

    @property
    def days(self) -> list[dict[str, Any]]:
        days: list[dict[str, Any]] = self.body["days"]
        return days


def repo_hash(salt: str, key: str) -> str:
    """The first 16 hex of ``HMAC-SHA256(salt, key)``: how a repository is named in a batch. The
    salt is this installation's own, so the same repository hashes differently for each user."""
    return hmac.new(bytes.fromhex(salt), key.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def install_method() -> str:
    """``brew``, ``pipx``, ``uv``, ``pip``, ``script`` (the one-line installer's uv tool) or
    ``unknown`` (a checkout)."""
    try:
        kind = install_route.detect().kind
    except Exception:
        return "unknown"
    return {
        install_route.HOMEBREW: "brew",
        install_route.PIPX: "pipx",
        install_route.UVX: "uv",
        install_route.UV_TOOL: "script",
        install_route.VENV: "pip",
        install_route.SYSTEM: "pip",
    }.get(kind, "unknown")


def client_info() -> dict[str, str]:
    """The installation's make and model: version, platform, architecture, interpreter."""
    release = ".".join(platform.release().split(".")[:2])
    return {
        "cli_version": __version__,
        "os": platform.system().lower() or "unknown",
        "os_version": release or "unknown",
        "arch": platform.machine().lower() or "unknown",
        "python": platform.python_version(),
        "install_method": install_method(),
    }


def _word(text: str, *, limit: int = 40) -> str:
    """``text`` as a short token of safe characters: what is sent as a NAME (a model, a plan)
    can only be one."""
    cleaned = _SAFE_NAME.sub("", text)[:limit]
    return cleaned or "unknown"


def day_body(rollup: DailyRollup, salt: str, *, signed_in: bool) -> dict[str, Any]:
    """One local day in the contract's shape. Repositories become hashes; of everything the
    rollup holds that names something of the user's, nothing remains."""
    features = sorted(rollup.features.items(), key=lambda item: (-item[1], item[0]))
    kept = dict(sorted(features[:MAX_FEATURES]))
    return {
        "date": rollup.date,
        "tz_offset_minutes": rollup.tz_offset_minutes,
        "active": True,
        "signed_in": signed_in,
        "tokens": rollup.tokens.model_dump(),
        "cost_usd_list": round(rollup.cost_usd_list, 4),
        "sessions": rollup.sessions,
        "turns": rollup.turns,
        "models": {
            _word(name, limit=64): counts.model_dump() for name, counts in rollup.models.items()
        },
        "accounts": {
            "active": rollup.accounts.active,
            "registered": rollup.accounts.registered,
            "by_type": {_word(kind, limit=32): n for kind, n in rollup.accounts.by_type.items()},
            "top_account_share": rollup.accounts.top_account_share,
        },
        "repos_active": rollup.repos_active,
        "repos": [
            {
                "repo_hash": repo_hash(salt, repo.key),
                "tokens_total": repo.tokens_total,
                "sessions": repo.sessions,
                "turns": repo.turns,
                "traits": {
                    "host": repo.traits.host,
                    "language": repo.traits.language or "unknown",
                    "size_bucket": repo.traits.size_bucket,
                    "monorepo": repo.traits.monorepo,
                },
            }
            for repo in rollup.repos[:MAX_REPOS]
        ],
        "features": kept,
        "fleet": {
            "agents_spawned": dict(sorted(rollup.fleet.agents_spawned.items())),
            "tasks_done": rollup.fleet.tasks_done,
        },
    }


def _digest(day: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(day, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_today(moment: datetime, tz: tzinfo | None) -> date:
    return moment.astimezone(tz).date()


def build_batch(
    *,
    now: datetime | None = None,
    tz: tzinfo | None = None,
    store: ContextStore | None = None,
    state: SharingState | None = None,
    identity: tuple[str, str] | None = None,
) -> Batch:
    """The body the next shipment would post: ``days`` holds today and the days of the last 31
    that are not yet acknowledged as they stand now, oldest first, as many as fit
    :data:`MAX_BODY_BYTES`.

    ``identity`` is ``(install id, salt)``; without it the installation's own is read, and one
    sharing never minted gets placeholders (nothing is stored by building a batch)."""
    moment = now or datetime.now(UTC)
    held = state if state is not None else sharing.read()
    if identity is None:
        identity = (
            (held.install_id, held.salt)
            if held.install_id and held.salt
            else (PLACEHOLDER_ID, secrets.token_hex(32))
        )
    install_id, salt = identity
    today = _local_today(moment, tz)
    rollups = usage_stats.daily_rollups(
        today - timedelta(days=WINDOW_DAYS - 1), today, now=moment, tz=tz, store=store
    )
    signed_in = iam.signed_in_quietly() is not None
    days: list[dict[str, Any]] = []
    digests: dict[str, str] = {}
    for rollup in rollups:
        body = day_body(rollup, salt, signed_in=signed_in)
        digest = _digest(body)
        if rollup.date == today.isoformat() or held.acked.get(rollup.date) != digest:
            days.append(body)
            digests[rollup.date] = digest
    batch = _batch(moment, install_id, days)
    while len(days) > 1 and len(json.dumps(batch)) > MAX_BODY_BYTES:
        digests.pop(days.pop(0)["date"], None)  # the oldest wait for the next shipment
        batch = _batch(moment, install_id, days)
    return Batch(body=batch, digests=digests)


def _batch(moment: datetime, install_id: str, days: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "install_id": install_id,
        "sent_at": _stamp(moment),
        "opt_out": False,
        "client": client_info(),
        "days": list(days),
    }


def preview(**kwargs: Any) -> str:
    """The batch the next shipment would post, as the JSON it is."""
    return json.dumps(build_batch(**kwargs).body, indent=2)


# --- identity ----------


def ensure_identity() -> tuple[str, str]:
    """This installation's ``(install id, salt)``, minted at the first opt-in and kept after."""

    def mint(state: SharingState) -> None:
        if not state.install_id:
            state.install_id = str(uuid.uuid4())
        if not state.salt:
            state.salt = secrets.token_hex(32)

    state = sharing.update(mint)
    assert state.install_id is not None and state.salt is not None
    return state.install_id, state.salt


def reset_identity() -> tuple[str, str]:
    """A new install id and salt: what was sent before is no longer this installation's."""

    def fresh(state: SharingState) -> None:
        state.install_id = str(uuid.uuid4())
        state.salt = secrets.token_hex(32)
        state.acked = {}
        state.linked = None
        state.last_sent_at = None

    state = sharing.update(fresh)
    assert state.install_id is not None and state.salt is not None
    return state.install_id, state.salt


# --- the shipment ----------


def endpoint() -> str:
    """Where batches go: the sign-in's API plus the contract's path."""
    return f"{iam.resolve_api_url()}{ENDPOINT_PATH}"


def _bearer(api_url: str) -> str | None:
    """The sign-in's token when there is one for THIS api: a session from another deployment is
    never sent here."""
    try:
        session = iam.current_session(api_url)
    except iam.IamError:
        return None
    if session is None or (session.source == "file" and session.api_url != api_url):
        return None
    return session.token


def ship(
    *,
    now: datetime | None = None,
    tz: tzinfo | None = None,
    store: ContextStore | None = None,
    transport: Transport | None = None,
    force: bool = False,
) -> ShipReport:
    """Post the next batch, unless sharing is off or forced off, a shipment is not due (``force``
    ignores that), another one is running, or there is nothing to send. Never raises."""
    reason = sharing.forced_off_reason()
    if reason is not None:
        return ShipReport(skipped=f"sharing is off: {reason}")
    if not sharing.switch_on():
        return ShipReport(skipped="sharing is off")
    moment = now or datetime.now(UTC)
    try:
        with locking.holding(sharing.lock_path(), wait_s=0):
            return _ship(moment, tz, store, transport or default_transport, force)
    except TimeoutError:
        return ShipReport(skipped="another shipment is running")
    except Exception as exc:  # a shipment may fail; it may not raise into whoever asked
        return ShipReport(error=f"{type(exc).__name__}: {exc}"[:200])


def _ship(
    moment: datetime, tz: tzinfo | None, store: ContextStore | None, post: Transport, force: bool
) -> ShipReport:
    state = sharing.read()
    if not force and not sharing.due(state, moment):
        return ShipReport(skipped=f"not due until {state.next_due_at}")
    identity = ensure_identity()
    state = sharing.read()
    batch = build_batch(now=moment, tz=tz, store=store, state=state, identity=identity)
    if not batch.days:
        _schedule(moment, success=True)
        return ShipReport(skipped="nothing to send")
    try:
        url = endpoint()
    except iam.IamError as exc:
        return _failed(moment, str(exc))
    token = _bearer(iam.resolve_api_url())
    retried = False
    try:
        result = post(url, batch.body, token, TIMEOUT_S)
        if result.status == 401 and token is not None:
            # A token the server will not take must not cost the shipment: it is accepted
            # anonymously. The days stay unlinked until the user signs in again.
            result = post(url, batch.body, None, TIMEOUT_S)
            retried = True
    except iam.IamError as exc:
        return _failed(moment, str(exc))
    if result.status == 200 and isinstance(result.body, dict):
        linked = bool(result.body.get("linked"))
        _acknowledge(moment, batch, linked)
        accepted = result.body.get("accepted_days")
        days = accepted if isinstance(accepted, int) else len(batch.days)
        return ShipReport(sent=True, days=days, linked=linked, retried_anonymously=retried)
    wait = result.retry_after() if result.status == 429 else None
    detail = _detail(result)
    return _failed(moment, f"HTTP {result.status}{detail}", retry_after=wait, retried=retried)


def _detail(result: iam.HttpResult) -> str:
    body = result.body
    if isinstance(body, dict) and body.get("errors") is not None:
        return f": {json.dumps(body['errors'])[:160]}"
    return ""


def _acknowledge(moment: datetime, batch: Batch, linked: bool) -> None:
    def record(state: SharingState) -> None:
        state.acked.update(batch.digests)
        cutoff = (
            moment.astimezone(UTC).date() - timedelta(days=sharing.KEEP_ACKED_DAYS)
        ).isoformat()
        state.acked = {day: digest for day, digest in state.acked.items() if day >= cutoff}
        state.last_sent_at = moment.isoformat(timespec="seconds")
        state.linked = linked
        state.failures = 0
        state.last_error = None
        state.next_due_at = (moment + sharing.SHIP_EVERY).isoformat(timespec="seconds")

    with contextlib.suppress(Exception):
        sharing.update(record)


def _schedule(moment: datetime, *, success: bool) -> None:
    def record(state: SharingState) -> None:
        state.next_due_at = (moment + sharing.SHIP_EVERY).isoformat(timespec="seconds")

    with contextlib.suppress(Exception):
        sharing.update(record)


def _failed(
    moment: datetime, error: str, *, retry_after: int | None = None, retried: bool = False
) -> ShipReport:
    def record(state: SharingState) -> None:
        state.failures += 1
        state.last_error = error[:200]
        delay = sharing.backoff(state.failures)
        if retry_after is not None:
            delay = max(delay, timedelta(seconds=retry_after))
        state.next_due_at = (moment + delay).isoformat(timespec="seconds")

    with contextlib.suppress(Exception):
        sharing.update(record)
    return ShipReport(error=error[:200], retried_anonymously=retried)


# --- the switch ----------


def enable(*, now: datetime | None = None) -> SharingStatus:
    """Turn sharing on: mint the identity (once), set the switch, record the consent, and make a
    shipment due. Raises :class:`SharingForcedOff` when something forces it off."""
    reason = sharing.forced_off_reason()
    if reason is not None:
        raise SharingForcedOff(reason)
    moment = now or datetime.now(UTC)
    ensure_identity()
    settings_service.set_value("sharing.enabled", "true")

    def record(state: SharingState) -> None:
        if state.consent_answer is None:
            state.consent_asked_at = moment.isoformat(timespec="seconds")
            state.consent_answer = "share"
        state.failures = 0
        state.last_error = None
        state.next_due_at = None
        state.spawned_at = None

    sharing.update(record)
    return status()


def disable(*, now: datetime | None = None, transport: Transport | None = None) -> ShipReport:
    """Turn sharing off: the switch, and what was kept of the shipments. If anything was ever
    sent, one ``opt_out`` ping says so, best effort; nothing is sent after it. Nothing is sent
    at all while something forces sharing off."""
    moment = now or datetime.now(UTC)
    state = sharing.read()
    settings_service.set_value("sharing.enabled", "false")

    def purge(old: SharingState) -> None:
        old.acked = {}
        old.next_due_at = None
        old.spawned_at = None
        old.failures = 0
        old.last_error = None
        old.linked = None
        old.last_sent_at = None

    sharing.update(purge)
    if state.last_sent_at is None or state.install_id is None:
        return ShipReport(skipped="nothing had been sent", opt_out=False)
    if sharing.forced_off_reason() is not None:
        return ShipReport(skipped="sharing is forced off: nothing is sent", opt_out=False)
    body = {
        "schema_version": SCHEMA_VERSION,
        "install_id": state.install_id,
        "sent_at": _stamp(moment),
        "opt_out": True,
        "client": client_info(),
        "days": [],
    }
    post = transport or default_transport
    try:
        result = post(endpoint(), body, None, TIMEOUT_S)
    except iam.IamError as exc:
        return ShipReport(error=str(exc)[:200], opt_out=True)
    except Exception as exc:
        return ShipReport(error=f"{type(exc).__name__}: {exc}"[:200], opt_out=True)
    if result.status == 200:
        return ShipReport(sent=True, opt_out=True)
    return ShipReport(error=f"HTTP {result.status}", opt_out=True)


def status() -> SharingStatus:
    """Where sharing stands: the switch, what forces it off, the last shipment, the sign-in."""
    state = sharing.read()
    reason = sharing.forced_off_reason()
    switch = sharing.switch_on()
    try:
        where: str | None = endpoint()
    except iam.IamError:
        where = None
    return SharingStatus(
        enabled=switch and reason is None,
        switch=switch,
        forced_off=reason,
        consent_asked=state.consent_answer is not None,
        consent_answer=state.consent_answer,
        install_id=state.install_id,
        signed_in=iam.signed_in_quietly() is not None,
        linked=state.linked,
        last_sent_at=sharing.parse_time(state.last_sent_at),
        next_due_at=sharing.parse_time(state.next_due_at),
        days_acknowledged=len(state.acked),
        failures=state.failures,
        last_error=state.last_error,
        endpoint=where,
    )


# --- the consent ----------


def consent_due() -> bool:
    """Whether the TUI should ask now: it has not been answered, sharing is not already on, and
    nothing forces it off (a forced-off machine is never asked)."""
    if sharing.forced_off_reason() is not None or sharing.switch_on():
        return False
    return sharing.read().consent_answer is None


def record_consent(answer: str, *, now: datetime | None = None) -> SharingStatus:
    """Record the answer of the one-time dialog: ``share`` turns sharing on, ``declined`` leaves
    it off and the dialog is not shown again."""
    moment = now or datetime.now(UTC)
    if answer == "share":
        return enable(now=moment)

    def record(state: SharingState) -> None:
        state.consent_asked_at = moment.isoformat(timespec="seconds")
        state.consent_answer = "declined"

    sharing.update(record)
    return status()
