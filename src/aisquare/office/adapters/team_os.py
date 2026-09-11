"""The local Team OS peer, read through exactly three endpoints.

Team OS is a **separate local process**, not a part of Office. It is stopped far
more often than it is running, so the shape of this module is decided by one
rule: *a stopped peer is an ordinary answer, never an outage of Office*. Every
method returns a :class:`~aisquare.office.models.ServiceResult`, the fleet
snapshot never depends on any of them, and nothing here can make a CLI agent
disappear.

**The allowed surface is three GETs.** ``/api/meta``, ``/api/roster`` and
``/api/views/{name}`` for an allow-listed ``name``. The peer also exposes
``/api/commands``, ``/api/run``, ``/api/chat``, ``/api/setup``, ``/api/propose``,
``/api/sync``, ``/api/file``, ``/api/account``, ``/api/graph`` and
``/api/identity``; those are mutations, engine invocations and an arbitrary file
reader, and none of them is reachable from this module by any code path. Team OS
is not a second fleet, task or action authority.

**Why ``cockpit`` is off the default allow-list.** Six view names exist, from the
``buildSnapshot`` if-chain at ``app/engine/snapshots.js:135-206``. Five are
enabled by default. ``cockpit`` is not, because its builder reads
``open-loops.md`` and ``waiting-on.md`` (``snapshots.js:173-174``) and
``app/engine/git.js:21`` lists both in ``PRIVATE_PATHS`` alongside ``people/``
and ``personal-brand/`` — so the view serves content the peer's own repository
marks private, behind a route name that gives no hint of it. It stays a name this
module knows how to decode, so an operator can enable it as an explicit choice;
it is not a default.

**Where the view names did *not* come from.** The peer's UI has an eleven-entry
``TABS`` array (``app/web/app.js:62-73``) and a ``CHIPS`` map keyed by tab id
(``app/engine/commands.js``). Six of those keys really are views, which is what
makes the other five — ``graph``, ``files``, ``chat``, ``open-loops``,
``waiting-on`` — such an easy mistake: they reach different routes entirely and
404 as views. :data:`KNOWN_VIEW_NAMES` is the builder chain and nothing else,
and a name outside it is refused at construction rather than at request time.

**Two facts about the peer that this module is built around.**

*The token rotates on its own.* ``app/server.js:31`` falls back to
``randomBytes`` when ``TEAMOS_TOKEN`` is unset in the peer's environment, so
restarting the peer silently invalidates a configured token. The resulting 401
is indistinguishable from a token that was always wrong — same body, no
``WWW-Authenticate`` header — so ``unauthorized`` here means "possibly just a
peer restart", and it **drops the cache**, because cached rows belonged to a
previous boot's authorisation.

*403 is not a permissions failure.* The origin/host gate runs before the token
gate (``app/server.js:210-211``), so a bad ``Host`` with no token yields 403 and
not 401. The peer has no per-resource authorisation at all. A 403 means Office
sent a header the peer refused: a client-configuration bug, not a credential
problem, and not retryable.

**What never leaves this module**: the token, in any form — no log, no error, no
cache key, no URL, no fixture, no serialized field; the peer's own error text,
which reflects caller input on 404 and embeds absolute paths on 500; and raw
peer objects, which are decoded into the contract's models or rejected.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol, TypeVar, cast

from aisquare.office.config import OfficeConfig
from aisquare.office.models import (
    Page,
    PageCoverage,
    PlatformQuery,
    ServiceError,
    ServiceErrorCode,
    ServiceResult,
    ServiceStatus,
    TeamOSMeta,
    TeamOSRosterEntry,
    TeamOSView,
    TeamOSViewSection,
    service_failed,
    service_ok,
)
from aisquare.office.ports import Clock

# --------------------------------------------------------------------------
# The allowed surface
# --------------------------------------------------------------------------

META_PATH: Final = "/api/meta"
ROSTER_PATH: Final = "/api/roster"
VIEW_PATH_PREFIX: Final = "/api/views/"

KNOWN_VIEW_NAMES: Final[tuple[str, ...]] = (
    "cockpit",
    "board",
    "team-pending",
    "calendar",
    "customers",
    "roadmap",
)
"""Every name ``buildSnapshot`` can answer (``app/engine/snapshots.js:135-206``).

``board`` and ``team-pending`` are **one builder reached by two names**
(``snapshots.js:138``) and the payload echoes whichever was requested. They are
two entries here on purpose: canonicalising one to the other would make the
response disagree with the request.
"""

DEFAULT_VIEW_ALLOW_LIST: Final[tuple[str, ...]] = (
    "board",
    "team-pending",
    "calendar",
    "customers",
    "roadmap",
)
""":data:`KNOWN_VIEW_NAMES` minus ``cockpit`` — see this module's docstring."""

TOKEN_HEADER: Final = "X-TEAMOS-TOKEN"
"""The only form the token is ever sent in.

The peer also accepts a ``token`` query parameter (``app/server.js:64-67``) and
its own UI uses that for ``<img>`` and download URLs, which is exactly how a
credential ends up in a log, a proxy trace and browser history. This module
never constructs a URL containing the token.
"""

MAX_BODY_BYTES: Final = 1_048_576
"""Bound applied *while reading*, before any decode, so an unbounded response
cannot be buffered in the first place."""

DEFAULT_CONNECT_TIMEOUT_S: Final = 1.0
"""Connect deadline, separate from the total. A stopped peer never waits this
out: a loopback connect to a closed port is refused by the kernel, so
``unavailable`` comes back in milliseconds."""

DEFAULT_CACHE_TTL_S: Final = 0.5
"""How long a successful record answers again without a second request.

It is also the coalescing window: two callers arriving together make **one**
bounded request, and the second is served the first's record, explicitly marked
``stale``.
"""

MAX_SECTION_CHARS: Final = 8000
MAX_SECTIONS: Final = 32
MAX_ROSTER_ROWS: Final = 200
MAX_LABEL_CHARS: Final = 120
MAX_REVISION_CHARS: Final = 64
_TRUNCATION_MARK: Final = "…"

_VIEW_TITLES: Final[Mapping[str, str]] = {
    "cockpit": "Cockpit",
    "board": "Board",
    "team-pending": "Team pending",
    "calendar": "Calendar",
    "customers": "Customers",
    "roadmap": "Roadmap",
}

_ROADMAP_BUCKETS: Final[tuple[tuple[str, str], ...]] = (
    ("now", "Now"),
    ("next", "Next"),
    ("later", "Later"),
    ("shipped", "Recently shipped"),
)


# --------------------------------------------------------------------------
# Transport seam
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PeerResponse:
    """One bounded HTTP response. Headers are deliberately not carried: nothing
    in the allowed surface depends on one, and the peer's ``Cache-Control`` is
    advice this module overrides with its own bounded, explicitly-stale cache."""

    status_code: int
    body: bytes


class TeamOSTransportError(Exception):
    """No HTTP response was produced.

    Never carries the underlying exception's text. The peer's failures embed
    absolute paths from its machine, and the whole point of this boundary is
    that such text has nowhere to travel.
    """


class TeamOSUnreachable(TeamOSTransportError):
    """The connection failed. The ordinary stopped-peer case."""


class TeamOSTimeout(TeamOSTransportError):
    """A deadline expired — connect, or the total covering the response body."""


class TeamOSBodyTooLarge(TeamOSTransportError):
    """The response exceeded the byte bound and was abandoned mid-read."""


class TeamOSTransport(Protocol):
    """One bounded GET. Implementations raise :class:`TeamOSTransportError`
    subclasses and never leak the peer's exception text."""

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        connect_timeout_s: float,
        total_timeout_s: float,
        max_bytes: int,
    ) -> PeerResponse:
        """Blocking. The caller runs this off the ASGI event loop."""


class HttpxTeamOSTransport:
    """The real transport, on the Office extra's HTTP client.

    Three choices are load-bearing rather than defaults:

    * ``trust_env=False`` — a loopback read must not be routed through a proxy
      an ambient environment variable happens to name.
    * ``follow_redirects=False`` — a redirect is the one way a request aimed at
      loopback could arrive somewhere else.
    * the body is **streamed**, with the total deadline and the byte bound
      checked per chunk. A per-operation timeout bounds the gap between bytes,
      not the exchange; three of the peer's routes can leave a request hanging
      with no response ever written (``app/server.js:215-216,222`` return a
      promise inside a ``try`` without awaiting it), so the deadline has to
      cover the response body or it does not cover the failure that exists.
    """

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        connect_timeout_s: float,
        total_timeout_s: float,
        max_bytes: int,
    ) -> PeerResponse:
        import httpx2

        started = time.monotonic()
        timeout = httpx2.Timeout(total_timeout_s, connect=connect_timeout_s)
        try:
            with (
                httpx2.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client,
                client.stream("GET", url, headers=dict(headers)) as response,
            ):
                body = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() - started > total_timeout_s:
                        raise TeamOSTimeout
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise TeamOSBodyTooLarge
                return PeerResponse(status_code=response.status_code, body=bytes(body))
        except httpx2.TimeoutException:
            raise TeamOSTimeout from None
        except httpx2.RequestError:
            raise TeamOSUnreachable from None


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


_T = TypeVar("_T")


class _Malformed(Exception):
    """A decoded body did not match the shape the contract admits."""


@dataclass(frozen=True, slots=True)
class _Identity:
    """What a cached record belongs to.

    ``token_digest`` is a **keyed** digest under a per-process salt, not the
    token and not a hash anybody could precompute. It is here because the peer
    rotates its token on every boot: the reference (the variable name) can stay
    identical while the value behind it changes, and a cache keyed on the name
    alone would keep serving a previous boot's authorised data.
    """

    base_url: str
    profile: str | None
    token_digest: str


@dataclass(frozen=True, slots=True)
class _Provenance:
    """Internal only, and never serialized: which endpoint answered, and for
    which named view. The browser-facing provenance is ``TeamOSView.name``."""

    path: str
    view_name: str | None


@dataclass(frozen=True, slots=True)
class _Cached:
    data: object
    observed_at: datetime
    monotonic_at: float
    provenance: _Provenance


@dataclass(frozen=True, slots=True)
class _Read:
    """Either a decoded JSON object, or a typed failure. Never both."""

    payload: Mapping[str, object] | None
    status: ServiceStatus
    error: ServiceError | None


_NO_DATA_STATUSES: Final[frozenset[str]] = frozenset({"unauthorized", "forbidden", "unconfigured"})
"""Statuses the envelope forbids from carrying data at all — losing
authorisation drops the cached body rather than painting it one more time."""


def _error(code: ServiceErrorCode, detail: str, *, retryable: bool) -> ServiceError:
    """A sanitised failure. ``detail`` is written here, never quoted from the peer."""
    return ServiceError(code=code, detail=detail, retryable=retryable)


def _bounded(text: str, limit: int) -> str:
    """``text`` within ``limit`` characters, truncation made visible."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + _TRUNCATION_MARK


def _optional_label(value: object, limit: int) -> str | None:
    """A bounded string, or None for anything that is not usable as one.

    Tolerant on purpose. Nothing was ever observed on this peer's wire, so a
    field that source-reading suggests is always written may still be absent,
    and an optional label is not worth failing a whole read over.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return _bounded(stripped, limit) if stripped else None


def _text(value: object) -> str:
    """One cell as display text. Non-strings become their JSON spelling."""
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _section(title: str, lines: Iterable[str]) -> TeamOSViewSection:
    """One bounded plain-text section. Never HTML, never a template."""
    body = "\n".join(line for line in lines)
    return TeamOSViewSection(
        title=_bounded(title, MAX_LABEL_CHARS), text=_bounded(body, MAX_SECTION_CHARS)
    )


def _rows(payload: Mapping[str, object], key: str) -> list[Mapping[str, object]]:
    """A list-of-objects field, or a refusal. Absent is an empty list."""
    value = payload.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise _Malformed
    out: list[Mapping[str, object]] = []
    for row in value:
        if not isinstance(row, Mapping):
            raise _Malformed
        out.append(row)
    return out


def _loop_line(row: Mapping[str, object]) -> str:
    """One accountability-ledger row as a single bounded line.

    ``overdue`` and ``age`` are the **peer's** computations against the
    **peer's** clock (``snapshots.js:75-77``). They are passed through as the
    peer's words and never recomputed here, because a second opinion about
    another process's dates is a guess wearing a number.
    """
    parts = [part for part in (_text(row.get("id")), _text(row.get("status"))) if part]
    owner = _text(row.get("owner"))
    item = _text(row.get("item"))
    head = " · ".join(parts)
    body = f"{owner} — {item}" if owner and item else owner or item
    line = f"{head} · {body}" if head and body else head or body
    due = _text(row.get("due"))
    if due:
        line += f" (due {due})"
    if row.get("overdue") is True:
        line += " [overdue]"
    notes = _text(row.get("notes"))
    if notes:
        line += f" — {notes}"
    return _bounded(line, 400)


# --------------------------------------------------------------------------
# The adapter
# --------------------------------------------------------------------------


class TeamOSAdapter:
    """P01's :class:`~aisquare.office.ports.TeamOSClient`, over loopback HTTP.

    Synchronous, like the port: the calls block, and the application runs them
    on its bounded executor rather than on the event loop.
    """

    def __init__(
        self,
        config: OfficeConfig,
        clock: Clock,
        *,
        transport: TeamOSTransport | None = None,
        env: Mapping[str, str] | None = None,
        allowed_views: Sequence[str] = DEFAULT_VIEW_ALLOW_LIST,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
        cache_ttl_s: float = DEFAULT_CACHE_TTL_S,
        stale_max_age_s: float = 0.0,
        max_body_bytes: int = MAX_BODY_BYTES,
        roster_id_salt: bytes | None = None,
    ) -> None:
        """Resolve the peer, the budgets and the allow-list.

        ``env`` is injected rather than read, exactly as ``OfficeConfig`` is:
        the config holds ``team_os_token_env``, the *name* of the variable, and
        the value behind it is resolved at the moment of each request so a
        rotated token is picked up without a restart.

        ``stale_max_age_s`` defaults to ``0.0`` — no stale serving. Showing an
        outage as data is opt-in, and when enabled the result says so: status
        ``partial``, ``stale`` true, the original ``observed_at``, and the
        outage carried in ``error`` rather than hidden behind it.
        """
        if config.team_os_host not in ("127.0.0.1", "localhost"):  # pragma: no cover
            raise ValueError("Team OS must be a loopback peer")
        unknown = [name for name in allowed_views if name not in KNOWN_VIEW_NAMES]
        if unknown:
            raise ValueError(
                f"not Team OS view names: {', '.join(sorted(unknown))}. "
                f"The peer's builders answer exactly {', '.join(KNOWN_VIEW_NAMES)} — "
                "the UI's tab list and its chips map also contain graph, files, chat, "
                "open-loops and waiting-on, which are different routes and 404 as views."
            )
        self._config = config
        self._clock = clock
        self._transport: TeamOSTransport = (
            transport if transport is not None else HttpxTeamOSTransport()
        )
        self._env: Mapping[str, str] = env if env is not None else {}
        self._allowed_views: tuple[str, ...] = tuple(dict.fromkeys(allowed_views))
        self._connect_timeout_s = connect_timeout_s
        self._cache_ttl_s = cache_ttl_s
        self._stale_max_age_s = stale_max_age_s
        self._max_body_bytes = max_body_bytes
        if roster_id_salt is None:
            # Function scope on purpose. ``secrets`` drags in ``hmac`` and
            # ``random``, and tests/test_iam_single_reader.py ratchets those out
            # of module scope so the hook path never pays for an import only an
            # Office adapter needs.
            import secrets

            roster_id_salt = secrets.token_bytes(16)
        self._roster_id_salt = roster_id_salt

        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._cache: dict[str, _Cached] = {}
        self._last_success: datetime | None = None
        self._identity: _Identity | None = None
        self._generation = 0

    # -- public surface ----------------------------------------------------

    @property
    def allowed_views(self) -> tuple[str, ...]:
        """The names this back-end will request. Server-side, never the browser's."""
        return self._allowed_views

    def meta(self) -> ServiceResult[TeamOSMeta]:
        """The peer's own description, and whether it answered at all.

        Unique among the three: a failure still produces **data**, because
        ``available: false`` with an empty ``views`` list is the contract's own
        spelling of a stopped peer (``team-os.json`` ``TeamOSMeta``), and it is
        more use to a UI than a null. The exception is the three statuses the
        envelope forbids from carrying data — those return null, and that
        asymmetry is deliberate.

        ``synced_at`` is not mapped at all. ``git.js:39`` hardcodes it null on
        this route and only ``POST /api/sync`` ever sets it, so there is no
        freshness signal here to pass on and pretending otherwise would invent
        one. ``version``, ``engine`` and ``lastCommitDate`` are dropped too:
        the schema admits five fields and the rest are redacted.
        """
        return self._perform(
            META_PATH,
            cache_key="meta",
            view_name=None,
            decode=self._decode_meta,
            on_failure=self._meta_failure,
        )

    def roster(self, query: PlatformQuery | None = None) -> ServiceResult[Page[TeamOSRosterEntry]]:
        """Peer identities, in their own namespace.

        Rows arrive as ``{name, email, slack_id}`` with **no identifier of any
        kind** (``identity.js:41``), so ``peer_local_id`` is minted here and is
        never an Office fleet agent id or session id. A row cannot create,
        rename, stop, freeze or assign a CLI agent, and a name that resembles
        one proves nothing.

        ``email`` and ``slack_id`` are read to distinguish rows and then
        discarded — the contract admits ``label`` and ``summary`` and neither is
        an address, so personal contact data does not cross this boundary.

        ``query`` is accepted for the port's signature. The peer paginates
        nothing, so there is nothing for a cursor to continue and
        ``next_cursor`` is always null.

        An empty roster is **valid data**. ``identity.js:73`` catches everything
        and returns ``[]``, so a missing ``CLAUDE.md`` reads as a successful
        empty page — and a connection failure must never be flattened into the
        same shape.
        """
        del query
        return self._perform(
            ROSTER_PATH,
            cache_key="roster",
            view_name=None,
            decode=self._decode_roster,
            on_failure=None,
        )

    def view(self, name: str) -> ServiceResult[TeamOSView]:
        """One allow-listed named view.

        A name outside the allow-list sends **no request at all** and reports
        ``unsupported``. The name is matched, never forwarded: the peer's router
        takes everything after ``/api/views/`` as the name, so forwarding would
        hand a browser control of the request path.
        """
        if name not in self._allowed_views:
            return service_failed(
                "unsupported",
                _error(
                    "unsupported_capability",
                    "That Team OS view is not enabled for this Office.",
                    retryable=False,
                ),
                last_success_at=self._last_success,
            )
        return self._perform(
            VIEW_PATH_PREFIX + name,
            cache_key=f"view:{name}",
            view_name=name,
            decode=lambda payload: self._decode_view(name, payload),
            on_failure=None,
        )

    # -- request pipeline --------------------------------------------------

    def _perform(
        self,
        path: str,
        *,
        cache_key: str,
        view_name: str | None,
        decode: Callable[[Mapping[str, object]], _T],
        on_failure: Callable[[ServiceStatus, ServiceError], ServiceResult[_T]] | None,
    ) -> ServiceResult[_T]:
        identity, generation = self._current_identity()
        with self._lock_for(cache_key):
            fresh = self._fresh(cache_key)
            if fresh is not None:
                return self._from_cache(fresh, stale=True, status="ok", error=None)

            read = self._fetch(path)
            now = self._clock.now()

            if read.status != "ok" or read.payload is None:
                error = read.error
                if error is None:  # pragma: no cover - _Read's own invariant
                    error = _error("internal", "Team OS read failed.", retryable=False)
                return self._failure(cache_key, read.status, error, on_failure)

            try:
                data = decode(read.payload)
            except _Malformed:
                malformed = _error(
                    "upstream_invalid",
                    "Team OS returned a response Office could not read.",
                    retryable=False,
                )
                return self._failure(cache_key, "unavailable", malformed, on_failure)

            self._store(
                cache_key,
                data,
                now,
                generation,
                identity,
                _Provenance(path=path, view_name=view_name),
            )
            return service_ok(data, observed_at=now, last_success_at=now)

    def _fetch(self, path: str) -> _Read:
        """One bounded GET, mapped to a status. No peer text survives this."""
        if not self._config.team_os_enabled:
            return _Read(
                None,
                "unconfigured",
                _error(
                    "service_unconfigured",
                    "The Team OS peer is not enabled for this Office.",
                    retryable=False,
                ),
            )
        token = self._token()
        if not token:
            return _Read(
                None,
                "unconfigured",
                _error(
                    "service_unconfigured",
                    "No Team OS token is configured for this Office.",
                    retryable=False,
                ),
            )

        headers = {TOKEN_HEADER: token, "Accept": "application/json"}
        # No Origin header: the peer only inspects one when present
        # (app/server.js:61), so omitting it is strictly safer than sending a
        # correct one.
        try:
            response = self._transport.get(
                self._config.team_os_base_url + path,
                headers=headers,
                connect_timeout_s=self._connect_timeout_s,
                total_timeout_s=self._config.remote_timeout_s,
                max_bytes=self._max_body_bytes,
            )
        except TeamOSTimeout:
            return _Read(
                None,
                "unavailable",
                _error("timeout", "Team OS did not answer in time.", retryable=True),
            )
        except TeamOSBodyTooLarge:
            return _Read(
                None,
                "unavailable",
                _error(
                    "upstream_invalid",
                    "Team OS returned more data than Office will read.",
                    retryable=False,
                ),
            )
        except TeamOSTransportError:
            return _Read(
                None,
                "unavailable",
                _error("service_unavailable", "Team OS is not reachable.", retryable=True),
            )
        except Exception:  # a transport bug is not an Office outage
            return _Read(
                None,
                "unavailable",
                _error("internal", "Office could not complete a Team OS read.", retryable=False),
            )

        return self._interpret(response)

    def _interpret(self, response: PeerResponse) -> _Read:
        code = response.status_code
        if code == 401:
            # Indistinguishable from a peer restart, which is why this is not
            # reported as an operator mistake and why it drops the cache.
            return _Read(
                None,
                "unauthorized",
                _error(
                    "unauthorized",
                    "Team OS rejected Office's token. The peer regenerates it on "
                    "every restart unless TEAMOS_TOKEN is set in its environment.",
                    retryable=False,
                ),
            )
        if code == 403:
            return _Read(
                None,
                "forbidden",
                _error(
                    "forbidden",
                    "Team OS refused the request's host or origin. This is an Office "
                    "client configuration problem, not a permissions one.",
                    retryable=False,
                ),
            )
        if code == 404:
            return _Read(
                None,
                "unsupported",
                _error(
                    "unsupported_capability",
                    "Team OS no longer serves that read.",
                    retryable=False,
                ),
            )
        if code != 200:
            return _Read(
                None,
                "unavailable",
                _error("upstream_error", "Team OS returned an error.", retryable=True),
            )
        if len(response.body) > self._max_body_bytes:
            return _Read(
                None,
                "unavailable",
                _error(
                    "upstream_invalid",
                    "Team OS returned more data than Office will read.",
                    retryable=False,
                ),
            )
        try:
            payload = json.loads(response.body)
        except (ValueError, UnicodeDecodeError):
            return _Read(
                None,
                "unavailable",
                _error(
                    "upstream_invalid",
                    "Team OS returned a response Office could not read.",
                    retryable=False,
                ),
            )
        if not isinstance(payload, Mapping):
            return _Read(
                None,
                "unavailable",
                _error(
                    "upstream_invalid",
                    "Team OS returned a response Office could not read.",
                    retryable=False,
                ),
            )
        return _Read(payload, "ok", None)

    def _failure(
        self,
        cache_key: str,
        status: ServiceStatus,
        error: ServiceError,
        on_failure: Callable[[ServiceStatus, ServiceError], ServiceResult[_T]] | None,
    ) -> ServiceResult[_T]:
        if status in _NO_DATA_STATUSES:
            # Authorisation is gone or was never there. The cached body belonged
            # to it, so it goes too — a revoked peer must not keep painting a
            # previous boot's roster.
            self._drop_all()
            if on_failure is not None:
                return on_failure(status, error)
            return service_failed(status, error, last_success_at=self._last_success)

        stale = self._stale(cache_key)
        if stale is not None:
            return self._from_cache(stale, stale=True, status="partial", error=error)
        if on_failure is not None:
            return on_failure(status, error)
        return service_failed(status, error, last_success_at=self._last_success)

    def _meta_failure(
        self, status: ServiceStatus, error: ServiceError
    ) -> ServiceResult[TeamOSMeta]:
        """A failed ``meta`` still says something true: the peer did not answer."""
        if status in _NO_DATA_STATUSES:
            return service_failed(status, error, last_success_at=self._last_success)
        now = self._clock.now()
        return ServiceResult[TeamOSMeta](
            status=status,
            data=TeamOSMeta(available=False, views=()),
            stale=False,
            observed_at=now,
            last_success_at=self._last_success,
            error=error,
        )

    # -- decoding ----------------------------------------------------------

    def _decode_meta(self, payload: Mapping[str, object]) -> TeamOSMeta:
        return TeamOSMeta(
            available=True,
            views=self._allowed_views,
            display_name=_optional_label(payload.get("branch"), MAX_LABEL_CHARS),
            peer_revision=_optional_label(payload.get("commit"), MAX_REVISION_CHARS),
        )

    def _decode_roster(self, payload: Mapping[str, object]) -> Page[TeamOSRosterEntry]:
        rows = _rows(payload, "roster")
        entries: list[TeamOSRosterEntry] = []
        for row in rows[:MAX_ROSTER_ROWS]:
            label = _optional_label(row.get("name"), MAX_LABEL_CHARS)
            if label is None:
                # No usable display label. Skipped rather than invented, and not
                # a malformed response: the peer's parser is a Markdown table
                # reader that admits rows this one cannot render.
                continue
            entries.append(
                TeamOSRosterEntry(peer_local_id=self._peer_local_id(row), label=label, summary=None)
            )
        truncated = len(rows) > MAX_ROSTER_ROWS
        return Page[TeamOSRosterEntry](
            items=tuple(entries),
            next_cursor=None,
            partial=truncated,
            coverage=PageCoverage(
                reported_total=min(len(rows), 1_000_000),
                total_is_exact=True,
                reachable_scope_count=1,
                failed_scope_count=0,
                omitted_scope_count=0,
            ),
        )

    def _peer_local_id(self, row: Mapping[str, object]) -> str:
        """A row's id in Team OS's own namespace, minted here.

        Keyed, so the digest of a work email cannot be recomputed by anyone
        holding a guess at the address — the salt never leaves the process. The
        cost is that ids change when Office restarts, which is honest: the peer
        has no stable identifier to preserve, and pretending otherwise would
        invite exactly the fleet join this module exists to prevent.
        """
        material = "\x00".join(_text(row.get(key)) for key in ("name", "email", "slack_id")).encode(
            "utf-8"
        )
        digest = hashlib.blake2b(material, key=self._roster_id_salt, digest_size=16).hexdigest()
        return f"peer-{digest}"

    def _decode_view(self, name: str, payload: Mapping[str, object]) -> TeamOSView:
        echoed = payload.get("view")
        if not isinstance(echoed, str) or echoed != name:
            # The peer echoes the requested name (snapshots.js:142). A mismatch
            # means this body is not the answer to this request.
            raise _Malformed
        sections = self._view_sections(name, payload)
        return TeamOSView(
            name=name,
            title=_VIEW_TITLES.get(name, name),
            sections=tuple(sections[:MAX_SECTIONS]),
        )

    def _view_sections(self, name: str, payload: Mapping[str, object]) -> list[TeamOSViewSection]:
        if name in ("board", "team-pending"):
            return self._board_sections(payload)
        if name == "roadmap":
            return self._roadmap_sections(payload)
        if name == "customers":
            return self._customers_sections(payload)
        if name == "calendar":
            return self._calendar_sections(payload)
        if name == "cockpit":
            return self._cockpit_sections(payload)
        raise _Malformed  # pragma: no cover - the allow-list admits nothing else

    def _board_sections(self, payload: Mapping[str, object]) -> list[TeamOSViewSection]:
        return [
            _stats_section(payload.get("stats")),
            _section("Items", (_loop_line(row) for row in _rows(payload, "items"))),
        ]

    def _roadmap_sections(self, payload: Mapping[str, object]) -> list[TeamOSViewSection]:
        buckets = payload.get("buckets")
        if buckets is None:
            buckets = {}
        if not isinstance(buckets, Mapping):
            raise _Malformed
        sections: list[TeamOSViewSection] = []
        for key, title in _ROADMAP_BUCKETS:
            entries = buckets.get(key)
            if entries is None:
                entries = []
            if not isinstance(entries, list):
                raise _Malformed
            sections.append(_section(title, (_text(entry) for entry in entries)))
        return sections

    def _customers_sections(self, payload: Mapping[str, object]) -> list[TeamOSViewSection]:
        # Keys are the Markdown table's headers lowercased verbatim
        # (snapshots.js:127-130), so they change whenever someone edits the
        # header row. Rendered as the peer spelled them rather than mapped onto
        # a fixed set this module would have to invent.
        return [
            _section("Directory", (_pairs(row) for row in _rows(payload, "directory"))),
            _section("Pipeline", (_pairs(row) for row in _rows(payload, "pipeline"))),
        ]

    def _calendar_sections(self, payload: Mapping[str, object]) -> list[TeamOSViewSection]:
        month = _optional_label(payload.get("month"), MAX_LABEL_CHARS)
        lines: list[str] = []
        for row in _rows(payload, "events"):
            parts = [part for part in (_text(row.get("date")), _text(row.get("track"))) if part]
            line = " · ".join(parts)
            label = _text(row.get("label"))
            if label:
                line = f"{line} · {label}" if line else label
            # The trailing release milestone carries no status key at all
            # (snapshots.js:201), unlike every ledger-derived event.
            status = _text(row.get("status"))
            if status:
                line += f" ({status})"
            lines.append(_bounded(line, 400))
        # The month is hardcoded in the peer (snapshots.js:202) and is not a
        # live calendar position, so it is labelled as the peer's own heading.
        return [
            _section("Month", [month] if month else []),
            _section("Events", lines),
        ]

    def _cockpit_sections(self, payload: Mapping[str, object]) -> list[TeamOSViewSection]:
        owe = [
            _bounded(
                f"{_text(row.get('item'))} → {_text(row.get('to'))} "
                f"(by {_text(row.get('by'))}, {_text(row.get('status'))})",
                400,
            )
            for row in _rows(payload, "owe")
        ]
        waiting = [
            _bounded(
                f"{_text(row.get('item'))} ← {_text(row.get('by'))} "
                f"({_text(row.get('status'))}"
                f"{', nudge' if row.get('nudge') is True else ''})",
                400,
            )
            for row in _rows(payload, "waiting")
        ]
        return [
            _stats_section(payload.get("stats")),
            _section("Overdue", (_loop_line(row) for row in _rows(payload, "overdue"))),
            _section("I owe", owe),
            _section("Waiting on", waiting),
        ]

    # -- identity, cache and locking ---------------------------------------

    def _token(self) -> str | None:
        variable = self._config.team_os_token_env
        if not variable:
            return None
        value = self._env.get(variable)
        return value.strip() if value else None

    def _current_identity(self) -> tuple[_Identity, int]:
        """The identity this call belongs to, invalidating everything on a change.

        The generation is returned with it so a request already in flight when
        the endpoint or token changed cannot write its answer into the new
        identity's cache.
        """
        token = self._token()
        digest = (
            hashlib.blake2b(
                token.encode("utf-8"), key=self._roster_id_salt, digest_size=16
            ).hexdigest()
            if token
            else "none"
        )
        identity = _Identity(
            base_url=self._config.team_os_base_url,
            profile=self._config.platform_profile,
            token_digest=digest,
        )
        with self._guard:
            if self._identity != identity:
                self._identity = identity
                self._cache.clear()
                self._generation += 1
            return identity, self._generation

    def _lock_for(self, cache_key: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(cache_key)
            if lock is None:
                lock = threading.Lock()
                self._locks[cache_key] = lock
            return lock

    def _fresh(self, cache_key: str) -> _Cached | None:
        """A record still inside the TTL, which is also the coalescing window."""
        with self._guard:
            cached = self._cache.get(cache_key)
        if cached is None:
            return None
        age = self._clock.monotonic() - cached.monotonic_at
        return cached if age < self._cache_ttl_s else None

    def _stale(self, cache_key: str) -> _Cached | None:
        """A record inside the configured outage tolerance, or nothing."""
        if self._stale_max_age_s <= 0:
            return None
        with self._guard:
            cached = self._cache.get(cache_key)
        if cached is None:
            return None
        age = self._clock.monotonic() - cached.monotonic_at
        return cached if age <= self._stale_max_age_s else None

    def _store(
        self,
        cache_key: str,
        data: object,
        now: datetime,
        generation: int,
        identity: _Identity,
        provenance: _Provenance,
    ) -> None:
        with self._guard:
            if generation != self._generation or self._identity != identity:
                # The endpoint or the token changed while this request was in
                # flight. Its answer belongs to an identity nobody is asking
                # about any more.
                return
            self._cache[cache_key] = _Cached(
                data=data,
                observed_at=now,
                monotonic_at=self._clock.monotonic(),
                provenance=provenance,
            )
            self._last_success = now

    def _drop_all(self) -> None:
        with self._guard:
            self._cache.clear()

    def _from_cache(
        self,
        cached: _Cached,
        *,
        stale: bool,
        status: ServiceStatus,
        error: ServiceError | None,
    ) -> ServiceResult[_T]:
        """A previously observed record, carrying its own observation time.

        ``observed_at`` stays the moment the peer answered, never now: the age
        of peer data is the whole reason a caller is told it is stale, and
        Office's own snapshot freshness is a separate clock entirely.
        """
        return ServiceResult[_T](
            status=status,
            data=cast(_T, cached.data),
            stale=stale,
            observed_at=cached.observed_at,
            last_success_at=self._last_success,
            error=error,
        )


def _stats_section(stats: object) -> TeamOSViewSection:
    """The peer's own counters, as the peer computed them."""
    if not isinstance(stats, Mapping):
        return _section("Stats", [])
    return _section("Stats", (f"{key}: {_text(value)}" for key, value in stats.items()))


def _pairs(row: Mapping[str, object]) -> str:
    """One dynamically-keyed row as ``key: value`` in the peer's own order."""
    return _bounded("; ".join(f"{key}: {_text(value)}" for key, value in row.items()), 400)


__all__ = [
    "DEFAULT_CACHE_TTL_S",
    "DEFAULT_CONNECT_TIMEOUT_S",
    "DEFAULT_VIEW_ALLOW_LIST",
    "KNOWN_VIEW_NAMES",
    "MAX_BODY_BYTES",
    "META_PATH",
    "ROSTER_PATH",
    "TOKEN_HEADER",
    "VIEW_PATH_PREFIX",
    "HttpxTeamOSTransport",
    "PeerResponse",
    "TeamOSAdapter",
    "TeamOSBodyTooLarge",
    "TeamOSTimeout",
    "TeamOSTransport",
    "TeamOSTransportError",
    "TeamOSUnreachable",
]
