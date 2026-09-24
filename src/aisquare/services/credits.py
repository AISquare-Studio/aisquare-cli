"""Workspace credits, as the API reports them, next to where traces land (#143).

Once a project points at a workspace (#142) the next question is always "how
much do I have left?". The backend answers it at ``GET /api/v2/credits/balance/``
for the workspace ``X-Workspace-Id`` names, and it accepts the CLI's sign-in
token there (measured 2026-09-13, AISquare-Studio-BE ``46944a0``:
``credits/v2/views/CreditBalanceView.py`` — ``IsAuthenticated``, default
authentication classes; the payload is built in ``credits/entitlements.py``).

The shape, verbatim from that code::

    {"period": "2026-09", "state": "ok" | "low" | "exhausted",
     "pools": {"run_credits":   {"daily": {used, limit, remaining, resets_at},
                                 "monthly": {...}, "state": ...},
               "build_credits": {...}},
     "costTrue": {...}}                      # only for cost-rated grants

``limit == -1`` (and then ``remaining == -1``) means unlimited. ``state`` is
the server's own verdict, so this module never re-derives "low" from numbers
it might read differently from the dashboard.

Rules, the same ones the Claude usage row follows (``services.claude_accounts``):
best effort — a changed endpoint or an expired session makes the row say
``credits unavailable`` with the reason and nothing else breaks; never called
from a hook or on any path a session pays for (the Accounts page's minute
tick, ``explainability status``, ``whoami`` and ``doctor --live`` are the only
callers); cached briefly on disk so a script that loops over ``status`` does
not turn into a poll.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from aisquare.core import paths
from aisquare.models import TraceDestination
from aisquare.services import iam

#: How long a reading is reused before the API is asked again.
CACHE_SECONDS = 60.0
#: The one request's ceiling: a status line must not hang on a slow API.
TIMEOUT_SECONDS = 5.0

_POOLS = (("run", "run_credits"), ("build", "build_credits"))
_WINDOWS = ("daily", "monthly")


@dataclass(frozen=True)
class Window:
    """One pool's window: ``used`` of ``limit`` (``None`` = unlimited), and when it resets."""

    used: float
    limit: float | None
    remaining: float | None
    resets_at: datetime | None = None

    @property
    def percent(self) -> float | None:
        """Used, 0-100, or ``None`` when the window is unlimited (nothing to fill)."""
        if self.limit is None or self.limit <= 0:
            return None
        return max(0.0, min(100.0, self.used / self.limit * 100))


@dataclass(frozen=True)
class WorkspaceCredits:
    """What ``credits/balance`` said for one workspace, or why it could not."""

    workspace_id: int
    workspace_name: str
    available: bool
    reason: str | None = None
    period: str | None = None
    state: str | None = None
    """The server's band: ``ok``, ``low`` or ``exhausted``."""
    windows: dict[str, Window] = field(default_factory=dict)
    """``run.daily``, ``run.monthly``, ``build.daily``, ``build.monthly`` — the ones present."""
    cost_true: dict[str, Any] | None = None
    fetched_at: datetime | None = None

    def window(self, pool: str, span: str) -> Window | None:
        return self.windows.get(f"{pool}.{span}")

    def as_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "workspace": {"id": self.workspace_id, "name": self.workspace_name},
            "available": self.available,
            "reason": self.reason,
            "period": self.period,
            "state": self.state,
            "pools": {
                key: {
                    "used": window.used,
                    "limit": window.limit,
                    "remaining": window.remaining,
                    "percent": window.percent,
                    "resets_at": window.resets_at.isoformat() if window.resets_at else None,
                }
                for key, window in self.windows.items()
            },
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
        }
        if self.cost_true is not None:
            payload["cost_true"] = self.cost_true
        return payload


# ── reading ───────────────────────────────────────────────────────────────────


def _number(value: Any) -> float | None:
    """A quota number; ``-1`` is the API's "unlimited" and reads as ``None``."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):  # an int past float's range, too
        return None
    if number < 0 or math.isnan(number):
        return None
    return number


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse(
    payload: Any, *, workspace_id: int, workspace_name: str, now: datetime
) -> WorkspaceCredits:
    """The balance payload as :class:`WorkspaceCredits`; tolerant, never raising.

    Tolerant by construction, for the same reason ``_billing_band`` in the
    ops module is: a shape this cannot read costs one row, not a command.
    """
    if not isinstance(payload, dict):
        return WorkspaceCredits(
            workspace_id, workspace_name, available=False, reason="unexpected balance shape"
        )
    pools = payload.get("pools") if isinstance(payload.get("pools"), dict) else {}
    windows: dict[str, Window] = {}
    for short, key in _POOLS:
        pool = pools.get(key) if isinstance(pools, dict) else None
        if not isinstance(pool, dict):
            continue
        for span in _WINDOWS:
            raw = pool.get(span)
            if not isinstance(raw, dict):
                continue
            used = _number(raw.get("used"))
            windows[f"{short}.{span}"] = Window(
                used=used if used is not None else 0.0,
                limit=_number(raw.get("limit")),
                remaining=_number(raw.get("remaining")),
                resets_at=_when(raw.get("resets_at")),
            )
    state = payload.get("state")
    cost = payload.get("costTrue")
    return WorkspaceCredits(
        workspace_id=workspace_id,
        workspace_name=workspace_name,
        available=True,
        period=str(payload["period"]) if payload.get("period") else None,
        state=str(state) if isinstance(state, str) else None,
        windows=windows,
        cost_true=cost if isinstance(cost, dict) else None,
        fetched_at=now,
    )


def fetch(
    session: iam.Session,
    *,
    workspace_id: int,
    workspace_name: str,
    workspace_uid: str | None = None,
    now: datetime | None = None,
) -> WorkspaceCredits:
    """One balance request for the workspace, as the signed-in user. Never raises.

    The workspace goes out as ``X-Workspace-Id`` (uid when known — the header's
    documented shape — else the id); the API answers 400 without a usable
    header, which is reported as a reason rather than treated as a session
    problem. A 401 here IS a session problem (the endpoint takes the token),
    so ``iam.request``'s own reading of it stands.
    """
    moment = now or datetime.now(tz=UTC)
    try:
        result = iam.request(
            "api/v2/credits/balance/",
            workspace=workspace_uid or str(workspace_id),
            api_url=session.api_url,
            tolerate=(400, 403, 404),
            timeout=TIMEOUT_SECONDS,
        )
    except iam.IamError as exc:
        return WorkspaceCredits(workspace_id, workspace_name, available=False, reason=exc.message)
    except HTTPException as exc:
        # What `urllib` raises from `http.client` rather than wrapping, and
        # `iam._http` does not catch: `IncompleteRead` for a body shorter than
        # its Content-Length, `LineTooLong`, a bad status line. It used to
        # escape to `status` and `whoami` as a traceback (review of #173,
        # round 1; ccf4ac8 closed the same hole on the root post).
        return WorkspaceCredits(
            workspace_id,
            workspace_name,
            available=False,
            reason=f"could not read the balance: {exc!r}",
        )
    if result.status != 200:
        detail = None
        if isinstance(result.body, dict):
            detail = result.body.get("error") or result.body.get("detail")
        return WorkspaceCredits(
            workspace_id,
            workspace_name,
            available=False,
            reason=f"HTTP {result.status}{f': {detail}' if detail else ''}",
        )
    return parse(result.body, workspace_id=workspace_id, workspace_name=workspace_name, now=moment)


# ── the brief cache ─────────────────────────────────────────────────────────────


def _cache_path(session: iam.Session, workspace_id: int) -> Path:
    """``credits/<host>[-<port>]-<workspace>-<credential>.json``.

    The host as ``iam._discovery_cache_path`` spells it — a port as ``-8123``,
    never ``:8123``, which on Windows names an NTFS alternate data stream
    rather than a file. And keyed by the credential that asked, as a short
    hash, never the token: a reading is what the API told THAT sign-in, and a
    second one within the minute (another user, an ``AISQUARE_TOKEN`` for
    another account) must ask for itself rather than be handed the first
    one's balance and ``cost_true`` (review of #173, round 1).
    """
    parsed = urlparse(session.api_url)
    host = (parsed.hostname or "unknown").replace(":", "_")
    port = f"-{parsed.port}" if parsed.port else ""
    credential = hashlib.sha256(session.token.encode("utf-8")).hexdigest()[:16]
    return paths.cache_dir() / "credits" / f"{host}{port}-{workspace_id}-{credential}.json"


def _cached(session: iam.Session, workspace_id: int, now: datetime) -> WorkspaceCredits | None:
    """The last reading, if it was taken within :data:`CACHE_SECONDS` of ``now``.

    Aged by the ``fetched_at`` the record carries, against the caller's clock —
    not by the file's mtime against the wall — so the two clocks the rest of
    this module uses are the only two in play (and a test can hold them still).

    A record of any other shape than :func:`_remember` writes (an older or
    newer CLI, a hand edit) is a miss, as ``iam.discover`` treats its own cache:
    refetched, never trusted. Every figure goes through ``float`` here, so one
    that would only fail when the row is drawn fails now instead.
    """
    try:  # the path under the guard too: a port urlparse cannot read is a ValueError
        raw = json.loads(_cache_path(session, workspace_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    fetched_at = _when(raw.get("fetched_at")) if isinstance(raw, dict) else None
    if fetched_at is None or (now - fetched_at).total_seconds() > CACHE_SECONDS:
        return None
    if (now - fetched_at).total_seconds() < 0:
        return None  # a reading from the future is a clock problem, not a cache hit
    try:
        windows = {
            str(key): Window(
                used=float(w["used"]),
                limit=None if w["limit"] is None else float(w["limit"]),
                remaining=None if w["remaining"] is None else float(w["remaining"]),
                resets_at=_when(w["resets_at"]),
            )
            for key, w in raw["windows"].items()
        }
        period, state, cost = raw.get("period"), raw.get("state"), raw.get("cost_true")
        return WorkspaceCredits(
            workspace_id=int(raw["workspace_id"]),
            workspace_name=str(raw["workspace_name"]),
            available=True,
            period=period if isinstance(period, str) else None,
            state=state if isinstance(state, str) else None,
            windows=windows,
            cost_true=cost if isinstance(cost, dict) else None,
            fetched_at=fetched_at,
        )
    except (KeyError, TypeError, AttributeError, ValueError, OverflowError):
        return None  # a damaged cache is refetched, never trusted


def _remember(session: iam.Session, credits: WorkspaceCredits) -> None:
    """Write the reading for the next minute; a cache that cannot be written costs nothing."""
    try:
        path = _cache_path(session, credits.workspace_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = asdict(credits)
        record["fetched_at"] = credits.fetched_at.isoformat() if credits.fetched_at else None
        for key, window in credits.windows.items():
            record["windows"][key]["resets_at"] = (
                window.resets_at.isoformat() if window.resets_at else None
            )
        path.write_text(json.dumps(record), encoding="utf-8")
    except (OSError, TypeError, ValueError):
        pass


def for_destination(
    session: iam.Session | None,
    destination: TraceDestination | None,
    *,
    now: datetime | None = None,
    use_cache: bool = True,
) -> WorkspaceCredits | None:
    """The credits of the workspace a project points at; ``None`` when there is nothing to ask.

    ``None`` (no session, or no destination) is different from "unavailable":
    the first means the row is not shown at all, the second that it is shown
    with its reason. A session for another host than the destination's is the
    first kind too — asking a different server about this workspace's id would
    be a wrong answer at best.
    """
    if session is None or destination is None:
        return None
    if session.api_url.rstrip("/") != destination.api_url.rstrip("/"):
        return None
    moment = now or datetime.now(tz=UTC)
    if use_cache:
        cached = _cached(session, destination.workspace_id, moment)
        if cached is not None:
            return cached
    credits = fetch(
        session,
        workspace_id=destination.workspace_id,
        workspace_name=destination.workspace_name,
        workspace_uid=destination.workspace_uid,
        now=moment,
    )
    if credits.available:
        _remember(session, credits)
    return credits


# ── one voice ───────────────────────────────────────────────────────────────────


def _amount(value: float) -> str:
    return f"{value:,.0f}" if value >= 10 or value == int(value) else f"{value:.1f}"


def describe_window(window: Window, *, now: datetime | None = None) -> str:
    """``120 of 500 left (24%) · resets in 3h 10m (18:00)`` or ``unlimited``."""
    from aisquare.cli.common import format_reset  # the one reset formatter (#152)

    if window.limit is None:
        return "unlimited"
    left = window.remaining
    if left is None:
        left = max(0.0, window.limit - window.used)
    percent_left = left / window.limit * 100 if window.limit else 0.0
    text = f"{_amount(left)} of {_amount(window.limit)} left ({percent_left:.0f}%)"
    reset = format_reset(window.resets_at, now=now)
    return f"{text} · resets {reset}" if reset else text


def describe(credits: WorkspaceCredits | None, *, now: datetime | None = None) -> str:
    """The one line every surface prints for a workspace's credits."""
    if credits is None:
        return "(no destination chosen)"
    if not credits.available:
        return f"{credits.workspace_name}: credits unavailable — {credits.reason or 'no reading'}"
    parts: list[str] = []
    for short, _key in _POOLS:
        daily = credits.window(short, "daily")
        monthly = credits.window(short, "monthly")
        if daily is None and monthly is None:
            continue
        spans = []
        if daily is not None:
            spans.append(f"today {describe_window(daily, now=now)}")
        if monthly is not None:
            spans.append(f"month {describe_window(monthly, now=now)}")
        parts.append(f"{short} credits: " + "; ".join(spans))
    if not parts:
        parts.append("no pools reported")
    band = f" [{credits.state}]" if credits.state and credits.state != "ok" else ""
    period = f" · period {credits.period}" if credits.period else ""
    return f"{credits.workspace_name}{band} — " + " · ".join(parts) + period
