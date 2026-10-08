"""The Remote Control server: one local port that shows the fleet to a phone, and acts on it.

``asq remote serve`` runs it in the foreground; the fleet UI's Remote modal (``R``)
runs it in a background thread through :func:`start_remote_server` /
:func:`stop_remote_server`. Either way it binds ``127.0.0.1:8750`` only (ngrok, or
the same machine's browser, is the only way in) and every path lives under
``/r/<token>/``: the page at ``/``, the JSON API under ``api/``, the live stream
at ``ws``. ``docs/remote.md`` is the full guide; this is the contract in brief.

**One choke point.** :class:`_TokenGate` runs five gates, in order, for every
HTTP request and every WebSocket handshake before any route sees it: the token
(a wrong one is a 404 on everything, so the URL alone leaks nothing), auto-off
(the same 404 once Remote has timed out), the ``Origin`` of a write or a socket
(403), the unlocked device behind the ``asq_remote`` cookie for every ``api/``
path but unlock and for the socket (401), and a 64 KiB cap on any body (413).
Routes read the device the gate found (:meth:`RemoteKit.kit_device`) and parse
bodies in one place (:meth:`RemoteKit.kit_json_object`).

**Reads** are exactly what ``asq --json`` prints: the handlers call the builders
the typer commands use (``projects_json``, ``agents_json``, ``board_json``, the
task and entry dumps), so nobody invents a field here. Each takes ``?project=``
(default: the CURRENT project; an unknown one is a 404 shaped like an unknown
agent), and each cached snapshot is keyed by it. Remote's own state is
``GET api/remote``.

**Writes** answer 403 until ``allow_write`` is switched on (default OFF, never on
by itself): ``POST api/{name}`` for the plan's writes and the agent actions, and
every lane route built with ``write_gated`` (:meth:`RemoteKit.kit_route`). A
write may carry a ``request_id``: a retry is answered from the request ledger
instead of running twice. Each write that goes through appends one line to
``remote-audit.log``. :data:`NOT_WRITE_GATED` lists the few routes that change
something without the gate, frozen.

**The stream** sends ``board``, ``fleet``, ``remote``, then ``needs_you``,
``action`` and a ``heartbeat``, then one ``pane`` frame per ``(project, label)``
subscription, each only when it changed.

**The lanes** live in their own modules and plug in through :class:`RemoteKit`:
``remote_needs`` (what needs the human), ``remote_push`` (Web Push),
``remote_actions`` (tell, stop, restart, switch, and the ledger). This module
imports them inside functions only, and none of them is on the hook path. A
lane that raises costs its own feature, never the server or a socket: a start
that fails is logged and the server runs without it, and a stream seam that
fails skips its frame for that tick.

State (token, password, ``allow_write``, ``auto_off_at``, devices) lives in
``~/.aisquare/remote.json``, owner-only (0600; on Windows, a DACL for this
account alone), and a serving process re-reads it when its bytes change, so
``aisquare remote allow-write on`` from another shell reaches the running
server within a second (:meth:`Runtime.reload_if_changed`). Everything that
touches real systems goes through :class:`Sources` and :class:`Writes`, two bags
of callables the tests replace: the server itself never opens the store or
spawns tmux.

Dependencies: starlette and uvicorn (already here through the ``serve`` extra) and
``websockets`` (uvicorn's WebSocket backend), the ``remote`` extra in pyproject.
All are imported lazily so this module, and the modal that imports it, load in a
base install; :func:`start_remote_server` and the CLI say what to install.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import hmac
import json
import logging
import math
import os
import re
import shutil
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from aisquare.core.atomic import Replacement, replacement
from aisquare.core.locking import lock_exclusive, unlock
from aisquare.core.paths import (
    despite_windows_contention,
    ensure_home,
    remote_audit_path,
    remote_dist_dir,
    remote_state_path,
    restrict_to_owner,
)
from aisquare.core.version import __version__
from aisquare.models import FleetAgent, FleetAgentStatus, ProjectInfo, TeamSession, TurnMetric

if TYPE_CHECKING:
    import socket
    from concurrent.futures import ThreadPoolExecutor

    import uvicorn
    from starlette.requests import HTTPConnection, Request
    from starlette.responses import Response
    from starlette.routing import Route
    from starlette.websockets import WebSocket

    from aisquare.core.tmux import Capture
    from aisquare.services.remote_actions import ActionLedger
    from aisquare.services.remote_needs import NeedsItem

log = logging.getLogger(__name__)

BIND = "127.0.0.1"
DEFAULT_PORT = 8750
"""Free on every branch in flight: ``serve`` holds 8747, cliXR (#178) 8748, the captain's
voice (#240) 8749. Two of them on one port would mean whichever starts second cannot."""
COOKIE = "asq_remote"
TICK_SECONDS = 1.0
UNLOCK_LIMIT = 5
UNLOCK_WINDOW_SECONDS = 60.0
"""Unlock attempts per client (uvicorn's resolved peer) per window, right or wrong."""
UNLOCK_GLOBAL_FAILURES = 20
UNLOCK_GLOBAL_WINDOW_SECONDS = 1_800.0
"""The global failed-unlock budget (:class:`UnlockBudget`): 20 wrong guesses in 30 min."""
KNOWN_DEVICE_FAILURES_MAX = 10
"""Wrong guesses a known device's cookie may send before the device is revoked."""
DEVICE_IDLE_LIMIT = timedelta(hours=24)
"""Unused this long, a device is signed out (its cookie refused, its record kept)."""
DEVICE_LIFETIME = timedelta(days=7)
"""After this long from its first unlock a device is removed; its cookie's Max-Age."""
DEVICE_UA_MAX = 200
DEVICE_ID = re.compile(r"dev_[0-9a-f]{8}\Z")
"""A device's public id, used ONLY with ``fullmatch``: ``dev_`` and eight hex digits."""
AUTO_OFF_EXTEND = timedelta(minutes=60)
AUTO_OFF_CEILING = timedelta(hours=8)
"""``POST api/remote/extend`` adds an hour, never past 8 h from now (SPEC §2.5)."""
AUTO_OFF_CHECK_SECONDS = 30.0
"""The longest ``serve``'s auto-off timer waits before it reads the wall clock again: what the
TUI's ``enforce_auto_off`` does every 30 s (:class:`_AutoOffTimer`)."""
STATE_VERSION = 2
"""``remote.json``'s format: 2 holds devices by id and cookie digest (SPEC §2.3)."""
STATE_LOCK_WAIT_SECONDS = 2.0
_LOCK_HELD = frozenset({errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES})
"""What ``flock`` and Windows' ``locking`` say when another process holds the lock."""
WS_CLOSE_UNAUTHORIZED = 4401
"""Close code the page keys on: after it the adapter routes to /unlock."""
WS_CLOSE_BAD_ORIGIN = 4403
"""A handshake from another origin, where the server offers no denial response."""
WS_CLOSE_NOT_FOUND = 4404
"""A wrong token (or Remote off), where the server offers no denial response."""
WS_CLOSE_REPLACED = 4409
"""The same device opened one socket too many and this, its oldest, made way: the page
does not reconnect it until its tab is visible again (SPEC §2.10)."""
WS_CLOSE_REMOTE_OFF = 4410
"""Remote was turned off on the machine, by its switch or by auto-off: the page says so and
waits, where 4401 would send it to an unlock that cannot succeed (SPEC §2.4)."""
WS_MAX_MESSAGE_BYTES = 65_536
"""The largest WebSocket message uvicorn accepts (``ws_max_size``), down from its 16 MiB."""

WS_CLIENT_MESSAGE_MAX = 4_096
"""The longest text frame a client may send; anything longer is ignored unread."""
WS_PANE_SUBSCRIPTIONS_MAX = 8
"""Panes one socket may watch at once; a 9th ``subscribe`` is refused with an error frame."""
WS_SOCKETS_PER_DEVICE = 4
"""Live sockets per device. A 5th closes the device's OLDEST (4409) rather than refusing the
new one: what a sleeping phone leaves behind is a half-open socket, and evicting it is what
lets the woken phone back in."""
PANE_CAPTURE_WORKERS = 4
"""Threads in the pool every pane capture of the stream runs on
(:meth:`RemoteKit.kit_pane_pool`)."""
HEARTBEAT_SECONDS = 10.0
"""How often a socket gets a ``heartbeat`` frame, changed or not, so the page can tell a quiet
fleet from a dead link (the default of ``build_app(heartbeat=)``)."""
CACHE_KINDS_MAX = 64
"""Snapshots the read cache keeps at once, however many ``?project=`` spellings are asked for
within one tick (:class:`_Cache`); a phone reads a handful."""

MAX_BODY_BYTES = 65_536
"""The largest body any request may carry, refused with 413 before a route sees it.

``unlock`` is covered too: it is the one body anyone holding only the URL can send.
Every legitimate body is a few hundred bytes, and the longest text a phone may type
still fits: ``agent/tell``'s 8 000 characters are at most 32 000 bytes of UTF-8 as
``JSON.stringify`` writes them, 48 000 if every one were a control character it
escapes as ``\\u00XX``."""

REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
"""A write's optional ``request_id``, used ONLY with ``fullmatch`` (``\\Z`` also guards a
later ``.match`` refactor): it keys the ledger and is shown back to the page."""

DEVICE_SCOPE = "asq_remote.device"
"""Where the gate leaves the request's :class:`Device`; :meth:`RemoteKit.kit_device` reads it."""

NOT_WRITE_GATED: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/unlock"),
        ("DELETE", "/api/devices/{device_id}"),  # own id = sign out; another id is gated inside
        ("POST", "/api/needs/dismiss"),
        ("POST", "/api/push/subscribe"),
        ("DELETE", "/api/push/subscription"),
        ("POST", "/api/push/test"),
    }
)
"""The ONLY routes that change something without the write gate (SPEC §1.2), frozen.

Each changes what the human is shown or who is signed in, never the fleet. A lane
that needs another entry asks for it: :meth:`RemoteKit.kit_route` refuses to build
an ungated write that is not listed, and ``tests/test_remote_gates.py`` walks the
built app for any that slipped past it."""

READ_ONLY_REASON = (
    "writes are off — on the machine run `aisquare remote allow-write on`, "
    "or switch Allow write actions in the R panel"
)
"""Every 403 ``read_only`` says this, and so do the page and the modal: the two ways to
turn writes on, both of which write the one switch in ``remote.json``."""
WRITE_ENDPOINTS = (
    "task/claim",
    "task/done",
    "note",
    "project/switch",
    "project/add",
    "project/remove",
    "send-keys",
)
"""PLAN §4-E, verbatim. Saturday's page wires exactly this list."""

INDEX_CACHE_CONTROL = "no-cache"
"""``index.html`` must be revalidated on every load, never heuristically cached.

With NO directive a browser is free to invent its own freshness lifetime from
``last-modified``, so a reload can keep a STALE index that names a PREVIOUS
build's hashed chunk — and every rebuild changes that hash, so the chunk it asks
for no longer exists. Measured against the human's own tunnel; it cost hours of
bug reports against a server that was perfectly healthy. ``no-cache`` does not
forbid storing, only using a stored copy without asking first.

MEASURED, so nobody assumes otherwise: starlette's ``FileResponse`` (1.6.0) sets
``etag`` and ``last-modified`` but does NOT honour ``If-None-Match``, so each
revalidation re-sends the document rather than answering 304. That is the whole
cost of this directive and it is a few KB per navigation, paid only on the index
— the hashed chunks beside it are never re-fetched at all. Correctness over a
saved kilobyte: a stale document is indistinguishable from broken code.
"""

ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
"""A YEAR, for content-addressed files only — the hash in the name IS the version."""

MUTABLE_CACHE_CONTROL = "no-cache"
"""Anything under ``assets/`` whose name carries no hash: revalidate it.

Marking an unhashed file immutable for a year would create exactly the bug this
fixes, one build later and with no way to flush it.
"""

_HASHED_ASSET = re.compile(r"-[A-Za-z0-9_-]{8,}$")
"""Vite's content hash — ``index-DfFvQnFu.js`` — matched on the stem."""

HISTORY_CAP = 5000
"""Most scrollback lines one ``?history=`` request may return (PLAN §4-L).

The fleet's tmux keeps ``history-limit 50000``, and a pane that deep must not be
able to make one request enormous. A request over this is served at the cap and
the response SAYS so (``history_capped``) rather than truncating quietly, so a
short answer is never mistaken for a short pane.
"""

INSTALL_HINT = "pip install 'aisquare-cli[remote]' (or: pipx inject aisquare-cli websockets)"

NO_PAGE_HINT = "the bundled remote page is missing from this install — reinstall aisquare-cli"
"""Shown by the modal's status line, ``asq remote serve``'s exit, and the raise of
``start_remote_server()`` — one sentence, so a broken install never runs a server that
quietly answers with nothing. A fresh machine never sees it: aisquare-cli carries its
own page (``services/remote_page.py``), served whenever none is installed."""

PASSPHRASE_WORDS = 4
"""Words in a passphrase: four distinct ones of :mod:`remote_words`' 512, about 36 bits."""

SEND_KEYS_TEXT_MAX = 2_048
"""The longest ``text`` one ``send-keys`` (or quick answer) types: at most 16 tmux calls of
``_HEX_CHUNK`` bytes even when every character is 4 bytes of UTF-8. Longer goes as a tell."""
SEND_KEYS_KEYS_MAX = 32
EXIT_KEY_REPEAT_SECONDS = 3.0
"""A second Ctrl-C (or Ctrl-D) to one agent this soon exits Claude Code: refused unless meant."""
EXIT_KEYS = frozenset({"C-c", "C-d"})
NOTE_TEXT_MAX = 8_000
NOTE_KINDS = frozenset({"note", "decision", "question", "result"})
"""The kinds a phone may post. The others (``attention``, ``limited``, ``agent_exited``,
``switched``…) are the fleet's own reports, which wake the manager or set an agent's state."""
PROJECT_ADD_PATH_MAX = 4_096

REMOTE_KEY_NAME = re.compile(
    r"(?:Enter|Escape|Tab|BTab|BSpace|Space|Up|Down|Left|Right|Home|End|PageUp|PageDown|Delete"
    r"|F(?:[1-9]|1[0-2])|C-[cdloru]|[0-9]|y|n)\Z"
)
"""Every key the pad sends and nothing else, used ONLY as ``REMOTE_KEY_NAME.fullmatch(key)``
(``\\Z`` also guards a later ``.match``). A control key is added by NAME, never as a range:
``C-z`` suspends Claude Code, and an ``M-`` key is an escape sequence, so neither is here."""
REMOTE_KEY_VOCABULARY = (
    "Enter, Escape, Tab, BTab, BSpace, Space, Up, Down, Left, Right, Home, End, PageUp, "
    "PageDown, Delete, F1-F12, C-c, C-d, C-l, C-o, C-r, C-u, 0-9, y, n"
)
_TEXT_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
"""What typed ``text`` may not hold: a C0 control other than tab, newline and carriage return,
or DEL (:func:`check_remote_text`)."""
_TEXT_CONTROL_KEYS = {
    "\x03": "C-c",
    "\x04": "C-d",
    "\x0c": "C-l",
    "\x0f": "C-o",
    "\x12": "C-r",
    "\x15": "C-u",
    "\x1b": "Escape",
    "\x08": "BSpace",
    "\x7f": "BSpace",
}
"""The pad's key for what a control character in ``text`` would have typed."""

AUDIT_DEVICE_MAX = 32
AUDIT_ENDPOINT_MAX = 32
AUDIT_SUMMARY_MAX = 300
"""How much of each field an audit line keeps (:func:`_audit_clean`)."""


class RemoteError(RuntimeError):
    """The server could not do what was asked (start, stop, revoke)."""


class RemoteUnavailable(RemoteError):
    """The ``remote`` extra is not installed."""


class NoRemotePage(RemoteError):
    """No built ``aisquare-remote`` page is installed at the directory the server would serve."""


class RequestError(Exception):
    """A handler's refusal, carried to the client as ``{error, message}``.

    ``audit`` is for a refusal that still did something: one that came after keys
    had reached an agent's pane, or after a fleet call that may have stopped the
    agent before it failed. The write dispatcher writes it to the audit log as it
    writes a success's summary.

    ``extra`` adds keys to that body: a 409 ``stale`` carries ``current``, what the
    agent shows now (SPEC §3.2). A handler the write dispatcher runs has no
    response of its own to put it in, so the refusal carries it.
    """

    def __init__(
        self, status: int, error: str, message: str, *, audit: str | None = None, **extra: object
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message
        self.audit = audit
        self.extra = extra

    def request_error_body(self) -> dict[str, object]:
        """The body the client gets: ``{error, message}``, then the extra keys."""
        return {**_error_body(self.error, self.message), **self.extra}


class NoSuchAgent(LookupError):
    """``panes/<agent>`` or ``send-keys`` named an agent the project does not have.

    Answered 404 ``no_such_agent``, as the agent actions answer the fleet's own
    ``NoSuchAgent``: the page reads it as "that agent is gone" and goes back to
    the fleet. Every other lookup that finds nothing is a 404 ``not_found``.
    """


class NoSuchProject(LookupError):
    """A ``?project=`` query or a write body's ``project`` field matches no project.

    ``fleet_service.resolve_project`` raises its own ``NoSuchProject`` (not a
    ``LookupError``) for both "no match" and "ambiguous" — folded into this one
    ``LookupError`` shape by :func:`_resolve_project` so every existing
    ``except LookupError`` (the GET routes, the write dispatcher, the WS tick)
    turns it into a 404 ``not_found`` — no new branch. (An agent that is gone is
    :class:`NoSuchAgent`, ``no_such_agent``.)
    """


def _resolve_project(ref: str | None) -> ProjectInfo:
    """The named project (id prefix, name or codename), or the CURRENT one for ``None``.

    Every read and write endpoint that used to hard-code
    ``fleet_service.resolve_project(None)`` — and so could only ever see the
    project the server started in — now threads an optional ``project`` through
    this one place.
    """
    from aisquare.services import fleet as fleet_service

    try:
        return fleet_service.resolve_project(ref)
    except fleet_service.NoSuchProject as exc:
        raise NoSuchProject(str(exc)) from exc


def _remote_now() -> datetime:
    return datetime.now(UTC)


def _stamp() -> str:
    return _remote_now().isoformat(timespec="seconds")


def new_token() -> str:
    """The 32-character path token — 24 random bytes, URL-safe."""
    import secrets  # kept off the hook path (tests/test_iam_single_reader.py)

    return secrets.token_urlsafe(24)


def new_password() -> str:
    """Four DISTINCT words of :data:`remote_words.REMOTE_PASSPHRASE_WORDS`, joined with ``-``."""
    import secrets

    from aisquare.services.remote_words import REMOTE_PASSPHRASE_WORDS

    return "-".join(secrets.SystemRandom().sample(REMOTE_PASSPHRASE_WORDS, PASSPHRASE_WORDS))


def normalize_passphrase(text: str) -> str:
    """A passphrase as a phone types it, as it is stored: ``Amber River, cedar`` is
    ``amber-river-cedar``. Lower case, every run of letters a word, joined with ``-``."""
    return "-".join(re.findall(r"[a-z]+", text.lower()))


def _same(supplied: str, expected: str) -> bool:
    """Constant-time equality on the UTF-8 bytes (str-mode rejects non-ASCII)."""
    import secrets

    return secrets.compare_digest(supplied.encode("utf-8", "replace"), expected.encode())


def _audit_clean(text: str, limit: int) -> str:
    """``text`` safe for one field of one audit line, at most ``limit`` characters.

    Every character that does not print is a ``?``: a newline or a carriage return
    would start a line of the sender's choosing, ``\\x1b`` would drive the terminal
    the log is read in, and NEL, U+2028/U+2029 and the bidi controls (format
    characters) reorder or break what a reader sees.
    """
    cleaned = "".join(ch if ch.isprintable() else "?" for ch in text)
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


def build_local_url(token: str, port: int = DEFAULT_PORT) -> str:
    return f"http://{BIND}:{port}/r/{token}/"


_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_NUMERIC_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]*")
"""A last label a browser reads as part of an IPv4 address (``https://0x7f.1`` is 127.0.0.1)."""


def check_public_origin(url: str) -> str:
    """``https://<host>`` for a public URL of this server; ``ValueError`` for anything else.

    https only, on a DNS name (never an IP literal, nor a name a browser reads
    as one), with no userinfo and no port but 443. A push link opens this
    origin, and the page there is where the human types the passphrase, so it
    is the most trusted string the server emits: it is taken only from the
    TUI's ngrok announcement or ``serve --public-url``, NEVER from a request
    header, which anyone who reaches the server writes (SPEC §5.8), and never
    from ngrok's local agent API, which any user of the machine can answer
    before the human's ngrok does. The path is dropped: ``/r/<token>/`` is the
    server's.
    """
    import ipaddress
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"{url!r} is not a URL") from exc
    if parts.scheme != "https":
        raise ValueError(f"{url!r} is not https")
    if "@" in parts.netloc:
        raise ValueError(f"{url!r} carries a user name")
    if port not in (None, 443):
        raise ValueError(f"{url!r} is on port {port}, not 443")
    host = parts.hostname or ""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError(f"{url!r} names an IP address, not a host")
    labels = host.split(".")
    if (
        len(labels) < 2
        or not all(_DNS_LABEL.fullmatch(label) for label in labels)
        or _NUMERIC_LABEL.fullmatch(labels[-1])
    ):
        raise ValueError(f"{url!r} does not name a DNS host")
    return f"https://{host}"


# --- state --------------------------------------------------------------------------


def _remote_instant(text: object, *, naive_is_local: bool = False) -> datetime | None:
    """An ISO stamp from ``remote.json`` as an aware datetime; ``None`` for anything else.

    Every stamp this module writes carries its offset. A naive one is a hand edit,
    or an ``auto_off_at`` an earlier build stored as the TUI's naive local time,
    which ``naive_is_local`` reads as local (SPEC §2.5); a device stamp reads as UTC.
    """
    if not isinstance(text, str) or not text:
        return None
    try:
        at = datetime.fromisoformat(text)
    except ValueError:
        return None
    if at.tzinfo is None:
        return at.astimezone() if naive_is_local else at.replace(tzinfo=UTC)
    return at


def _iso_seconds(at: datetime) -> str:
    """``at`` as the ISO UTC stamp, to the second, that ``remote.json`` and the API carry."""
    return at.astimezone(UTC).isoformat(timespec="seconds")


def _secret_digest(secret: str) -> str:
    """Hex SHA-256 of a cookie's secret: all ``remote.json`` keeps of it."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


@dataclass
class Device:
    """One unlocked browser, named by an id that is NOT its cookie (SPEC §2.3).

    The cookie is a 43-character secret only the browser holds; ``remote.json``
    keeps its SHA-256, so a leaked file replays no session. Everything that names a
    device (the API, the audit log, ``asq remote status``, the modal, the socket
    registry) names it by :attr:`id`, which is public and unlocks nothing: the
    sid that used to stand in for both was the cookie itself, listed to every other
    device by ``GET api/devices`` and printed by ``status``.
    """

    id: str
    """``dev_`` and eight hex digits, unique among the current devices."""
    secret_sha256: str
    """Hex SHA-256 of the cookie's secret, which a presented cookie is compared against."""
    ua: str
    """The browser's ``User-Agent``, every character that does not print a ``?``
    (:func:`_audit_clean`): ``status`` and the R panel print it on the machine's own
    terminal, and a header's bytes past 0x7f arrive as latin-1, C1 controls included."""
    first_seen: str
    last_seen: str
    """ISO UTC. Touched in memory on every request; the flush writes it every 30 s."""
    expires_at: str
    """ISO UTC, :data:`DEVICE_LIFETIME` after the first unlock. A re-unlock never moves it."""
    failed_unlocks: int = 0
    """Wrong passphrases sent with this device's cookie since it last unlocked (§2.2 item 4).
    Never shown anywhere: it is a count toward :data:`KNOWN_DEVICE_FAILURES_MAX`."""

    def device_expired(self, now: datetime) -> bool:
        """Past its lifetime: removed at the next prune, and refused until then."""
        expires = _remote_instant(self.expires_at)
        return expires is None or now >= expires

    def device_signed_in(self, now: datetime) -> bool:
        """Used within :data:`DEVICE_IDLE_LIMIT` and still within its lifetime.

        A device idle longer is SIGNED OUT, not removed: its cookie is refused, but
        the record stays until it expires, so its push subscription keeps working
        and the same phone re-unlocks into the same id (SPEC §2.4).
        """
        seen = _remote_instant(self.last_seen)
        return seen is not None and now - seen < DEVICE_IDLE_LIMIT and not self.device_expired(now)

    def device_json(self) -> dict[str, object]:
        """The record as ``remote.json`` keeps it: the digest, never the secret."""
        return {
            "id": self.id,
            "secret_sha256": self.secret_sha256,
            "ua": self.ua,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "expires_at": self.expires_at,
            "failed_unlocks": self.failed_unlocks,
        }

    def device_row(self, now: datetime) -> dict[str, object]:
        """The device as the API, ``asq remote status`` and the modal show it: no secret, no
        digest, no failure count."""
        return {
            "id": self.id,
            "ua": self.ua,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "expires_at": self.expires_at,
            "signed_in": self.device_signed_in(now),
        }

    @classmethod
    def from_json(cls, row: object) -> Device | None:
        """A record from ``remote.json``, or ``None`` for one that is not a v2 device."""
        if not isinstance(row, dict):
            return None
        device_id, digest = row.get("id"), row.get("secret_sha256")
        if not isinstance(device_id, str) or not DEVICE_ID.fullmatch(device_id):
            return None
        if not isinstance(digest, str) or not digest:
            return None
        failed = row.get("failed_unlocks")
        return cls(
            id=device_id,
            secret_sha256=digest,
            ua=str(row.get("ua") or ""),
            first_seen=str(row.get("first_seen") or ""),
            last_seen=str(row.get("last_seen") or ""),
            expires_at=str(row.get("expires_at") or ""),
            failed_unlocks=failed if isinstance(failed, int) and failed >= 0 else 0,
        )


@dataclass
class RemoteInfo:
    """What the modal shows: the link and the password (PLAN §4-F)."""

    token: str
    password: str
    url_local: str


@dataclass
class _State:
    """``remote.json``, version 2 (SPEC §2.3): devices by id and digest, never by cookie."""

    token: str
    password: str
    allow_write: bool = False
    auto_off_at: str | None = None
    devices: list[Device] = field(default_factory=list)
    unlock_failures: list[str] = field(default_factory=list)
    """When each wrong passphrase that counts against the global budget arrived (ISO UTC,
    oldest first, only those still inside its window): :class:`UnlockBudget` reads and
    writes them here, so every process sees one budget and a restart does not reset it."""

    def state_json(self) -> dict[str, object]:
        return {
            "version": STATE_VERSION,
            "token": self.token,
            "password": self.password,
            "allow_write": self.allow_write,
            "auto_off_at": self.auto_off_at,
            "devices": [device.device_json() for device in self.devices],
            "unlock_failures": list(self.unlock_failures),
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> _State:
        """A version-2 file; a missing token or password is made anew (and then written).

        Writes are on only for a JSON ``true``. ``bool()`` of a hand edit's ``"false"``,
        ``"off"`` or ``"no"`` is true: it opened every write, and the load wrote the file
        back saying ``true``.
        """
        token = raw.get("token")
        password = raw.get("password")
        rows = raw.get("devices")
        stamps = raw.get("unlock_failures")
        auto_off = raw.get("auto_off_at")
        return cls(
            token=token if isinstance(token, str) and token else new_token(),
            password=password if isinstance(password, str) and password else new_password(),
            allow_write=raw.get("allow_write") is True,
            auto_off_at=auto_off if isinstance(auto_off, str) else None,
            devices=[d for d in map(Device.from_json, rows if isinstance(rows, list) else []) if d],
            unlock_failures=[
                stamp
                for stamp in (stamps if isinstance(stamps, list) else [])
                if isinstance(stamp, str)
            ],
        )

    @classmethod
    def migrated(cls, raw: dict[str, Any]) -> _State:
        """A version-1 file, once (SPEC §2.2 item 10): the link and the switches carry over,
        the password does not, and neither does any session.

        A v1 password is four words of 32, about 19.7 bits, so a new one comes from
        the 512-word list. Every v1 session was stored as its raw cookie, so the file
        (and every audit line) held replayable sessions: all of them go, and each
        phone unlocks once more. Writes stay on only for a JSON ``true``, as in
        :meth:`from_json`.
        """
        token = raw.get("token")
        auto_off = raw.get("auto_off_at")
        return cls(
            token=token if isinstance(token, str) and token else new_token(),
            password=new_password(),
            allow_write=raw.get("allow_write") is True,
            auto_off_at=auto_off if isinstance(auto_off, str) else None,
        )


_UNRESTRICTED = (
    "remote: could not restrict %s to your account — other users on this machine "
    "may be able to read %s"
)
_BLANK_STATE = b" \t\r\n\x00"
"""All an empty ``remote.json`` holds: whitespace, or the NULs a crash leaves when the size
reached the disk and the data did not (``core.state_file`` reads its file the same way)."""


def _encoded_state(state: _State) -> bytes:
    """The exact bytes of ``remote.json`` for ``state``: what is written, and what a file
    already written this way compares equal to."""
    return json.dumps(state.state_json(), indent=2).encode("utf-8")


def _lock_state_file(path: Path) -> int | None:
    """``<path>.lock`` held exclusively, as a descriptor; ``None`` when it could not be had.

    Waits at most :data:`STATE_LOCK_WAIT_SECONDS`, polling, as ``core.state_file`` does.
    Past that, or when the lock file cannot be opened at all (a read-only home), the
    write goes ahead without it, logged: a write is an atomic rename, so the file is
    never torn, and what the lock prevents is one healthy writer undoing another; a
    holder stalled that long is not one, and refusing would turn its stall into a
    failed unlock or a crashed TUI.
    """
    lock_path = path.with_name(f"{path.name}.lock")
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        log.debug("remote: %s could not be opened (%s); writing without it", lock_path, exc)
        return None
    deadline = time.monotonic() + STATE_LOCK_WAIT_SECONDS
    while True:
        try:
            lock_exclusive(fd)
            return fd
        except OSError as exc:
            if exc.errno not in _LOCK_HELD or time.monotonic() >= deadline:
                os.close(fd)
                log.warning("remote: %s not taken (%s); writing without it", lock_path, exc)
                return None
            time.sleep(0.01)


class Runtime:
    """The server's mutable state: ``remote.json``, the live sockets, the audit log.

    Shared between the uvicorn thread and whoever called :func:`start_remote_server` (the
    TUI's thread), so every mutation takes the lock; and with every other process that
    writes ``remote.json`` (``asq remote revoke``, ``allow-write``,
    ``regenerate-password``), so every read-modify-write of the file also holds
    ``remote.json.lock`` and starts from what is on disk. ``allow_write`` is never
    flipped on here — only :meth:`set_allow_write` does, on an explicit call.
    """

    def __init__(self, state_path: Path, audit_path: Path) -> None:
        self._state_path = state_path
        self._audit_path = audit_path
        self._lock = threading.RLock()
        """Every read and change of the state in memory; the event loop takes it on every
        request, so it is never held while waiting on another process."""
        self._writing = threading.RLock()
        """One read-modify-write of ``remote.json`` at a time in this process, held while
        ``remote.json.lock`` is waited for (:meth:`_state_file_lock`). Taken before
        :attr:`_lock`, never while holding it."""
        self._closers: dict[str, set[Callable[[int], None]]] = {}
        """Each device's live sockets, by device id, as closers that take the close code."""
        self._file_lock_depth = 0
        self._unpublished: bytes | None = None
        """What the read-modify-write in hand decided to write (:meth:`_write_state`), until
        its outermost :meth:`_state_file_lock` publishes it."""
        self._disk: bytes | None = None
        """Digest of the file's bytes as this process last wrote or read them.

        A content fingerprint, not ``(mtime_ns, size)``: the modal coder measured
        195 of 200 same-size rewrites landing inside one mtime tick on this WSL2
        filesystem, so a regenerated passphrase of equal length went unnoticed.
        The file is a few hundred bytes; hashing it costs about what the stat did.
        """
        self._disk_moves = 0
        """How many times :attr:`_disk` has moved: every write this process renamed into
        place, and every file it adopted. A reload that read the file while it moved adopts
        nothing (:meth:`reload_if_changed`)."""
        self.reads = 0
        """How many times the file was parsed after startup — tests pin the short-circuit."""
        self._said_unrestricted = False
        self._public_origin: str | None = None
        """Where phones reach this server, as :func:`check_public_origin` passed it; memory only."""
        self._state = self._load_state()

    # -- persistence --

    @staticmethod
    def _state_digest(data: bytes) -> bytes:
        return hashlib.blake2b(data, digest_size=16).digest()

    def _signature(self) -> tuple[bytes, bytes] | None:
        """``(digest, bytes)`` of the file right now, or ``None`` when it cannot be read."""
        try:
            data = self._state_path.read_bytes()
        except OSError:
            return None
        return (self._state_digest(data), data)

    @contextlib.contextmanager
    def _state_file_lock(self) -> Iterator[None]:
        """``remote.json.lock``, then this runtime's lock, around one read-modify-write.

        The per-process lock alone never covered the CLI: a ``revoke`` or a
        ``regenerate-password`` that landed inside a server's flush was undone by
        it, and a ``status`` rewrote the file from its own snapshot over a device
        that had just unlocked. Re-entrant within this runtime, since a file lock
        taken twice by one process would wait on itself.

        The wait for the file lock holds :attr:`_writing` alone. Holding
        :attr:`_lock` through it, a write that waited on another process (up to
        :data:`STATE_LOCK_WAIT_SECONDS`) held up every request the event loop
        served meanwhile, since the gate reads the state under ``_lock``.

        The write itself is not done under either. Its owner-only temp is made,
        and restricted while still empty, before the file lock is asked for
        (``core.atomic.replacement``): on Windows the restriction is ``icacls``
        (and ``whoami`` the first time), seconds under an antivirus scan, and
        inside the lock it outlasted another process's wait, which then wrote
        without the lock and was undone by this write: a CLI ``revoke`` lost. A
        temp of this write's own needs no lock. What the read-modify-write
        decided (:meth:`_write_state`) is published once ``_lock`` is let go,
        still under the file lock, so its fsyncs and rename hold up no request
        and no socket tick. Until the rename this process's digest of the file
        stays the old one, so a reload meanwhile finds nothing new to adopt; and
        a reload that read the old file before the rename and compares after it
        adopts nothing either (:attr:`_disk_moves`).
        """
        with self._writing:
            if self._file_lock_depth:
                self._file_lock_depth += 1
                try:
                    with self._lock:
                        yield
                finally:
                    self._file_lock_depth -= 1
                return
            with contextlib.ExitStack() as made:
                pending: Replacement | None = None
                unmade: OSError | None = None
                try:
                    self._state_path.parent.mkdir(parents=True, exist_ok=True)
                    pending = made.enter_context(replacement(self._state_path, owner_only=True))
                except OSError as exc:  # a home it cannot write: raised if there is a write
                    unmade = exc
                fd = _lock_state_file(self._state_path)
                self._file_lock_depth = 1
                try:
                    try:
                        with self._lock:
                            yield
                    finally:
                        body, self._unpublished = self._unpublished, None
                        if body is not None:
                            self._publish_state(pending, unmade, body)
                finally:
                    self._file_lock_depth = 0
                    if fd is not None:
                        with contextlib.suppress(OSError):
                            unlock(fd)
                        os.close(fd)

    def reload_if_changed(self) -> bool:
        """Re-read ``remote.json`` if ANOTHER process changed it; ``True`` when it had.

        ``aisquare remote allow-write on``, ``regenerate-password`` and ``revoke``
        run in their own process and write the file; a serving process that only
        trusted memory kept answering with the old switches (measured: 30 s of
        ``allow_write:false`` after the toggle). One small read plus a blake2b
        digest per check is the whole cost; the file is parsed only when its
        BYTES differ from what this process last wrote or read. Not mtime: on
        this filesystem 195 of 200 same-size rewrites shared an mtime tick, which
        hid a regenerated passphrase of equal length. An unreadable or
        half-written file keeps the state in hand and is retried on the next change,
        and so does a version-1 file an older build wrote: this process's next
        write makes it version 2 again, with everything it holds.

        What applying the file means: switches, token and password replace (a new
        password also resets the failed-unlock budget); devices the file no longer
        lists are revoked here too (sockets closed with 4401); ``last_seen`` keeps
        the newer of memory and disk.

        The file is read before ``_lock`` is taken, so a write this process renames
        into place meanwhile can overtake the read. Compared after that write, the
        file it replaced looked like another process's change: adopting it dropped
        a device that had just unlocked, let a device just revoked make one more
        request, and closed the sockets of every device the old file did not list.
        So a read made while :attr:`_disk` moved, by a write here or by another
        reload's adoption, is never adopted: what is in hand is that newer state,
        and anything newer still on disk is read by the next check.
        """
        with self._lock:
            moves = self._disk_moves
        signature = self._signature()
        with self._lock:
            if signature is None or signature[0] == self._disk or moves != self._disk_moves:
                return False
            digest, data = signature
            try:
                raw = json.loads(data.decode("utf-8-sig"))
            except (ValueError, RecursionError):
                return False
            if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
                return False
            self.reads += 1
            self._disk = digest
            self._disk_moves += 1
            incoming = _State.from_json(raw)
            known = {device.id: device for device in self._state.devices}
            for device in incoming.devices:
                previous = known.get(device.id)
                if previous is not None and previous.last_seen > device.last_seen:
                    device.last_seen = previous.last_seen
            if incoming.password != self._state.password:
                incoming.unlock_failures = []
            kept = {device.id for device in incoming.devices}
            self._state = incoming
            for device_id in [device_id for device_id in known if device_id not in kept]:
                self._close_sockets(device_id, WS_CLOSE_UNAUTHORIZED)
            return True

    def _read_state_file(self) -> tuple[bytes, dict[str, Any]] | None:
        """The file's bytes and its JSON object; ``None`` when there is nothing to keep.

        Nothing to keep is no file, or one holding only blanks or a crash's NULs.
        Anything else that is not a JSON object is a :class:`RemoteError`, and so is
        a file that cannot be read: what replaces a file is a new link and a new
        passphrase, so every phone loses Remote, and a hand edit's typo did exactly
        that for any process that merely read the file (``asq remote status``, the
        R modal), the running server adopting it on its next request.
        """
        try:
            # On NTFS a read inside another process's rename over the file is refused
            # for the rename's width, an "Access is denied" that is no permission problem.
            data = despite_windows_contention(self._state_path.read_bytes)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RemoteError(
                f"{self._state_path} could not be read ({exc}) — nothing was changed; "
                "fix its permissions and try again"
            ) from exc
        if not data.strip(_BLANK_STATE):
            return None
        try:
            raw = json.loads(data.decode("utf-8-sig"))
        except (ValueError, RecursionError):
            raw = None
        if not isinstance(raw, dict):
            raise RemoteError(
                f"{self._state_path} is not a JSON object — nothing was changed; fix it, "
                "or move it aside to start over with a new link and password"
            )
        return data, raw

    def _load_state(self) -> _State:
        """``remote.json`` as it is; written only when there is something to write.

        A version-2 file that parses is taken as it is, with no write: every process
        that reads the file (``asq remote status``, every CLI toggle) used to rewrite
        it from its own snapshot, and one landing inside a server's unlock dropped
        the device that had just unlocked. A missing or empty file is made and a
        version-1 one migrated, each under the file lock and re-read there first, so
        two processes starting at once settle on one file; any other file is refused
        (:meth:`_read_state_file`), never replaced.
        """
        found = self._read_state_file()
        if found is not None and found[1].get("version") == STATE_VERSION:
            data, raw = found
            state = _State.from_json(raw)
            if _encoded_state(state) == data:
                self._disk = self._state_digest(data)
                return state
        with self._state_file_lock():
            found = self._read_state_file()
            if found is None:
                state = _State(new_token(), new_password())
            elif found[1].get("version") == STATE_VERSION:
                state = _State.from_json(found[1])
            else:
                state = _State.migrated(found[1])
            self._write_state(state)
            return state

    def _write_state(self, state: _State) -> None:
        """Have ``remote.json`` replaced with ``state`` when the outermost
        :meth:`_state_file_lock` ends; callers hold it. The last state handed over in
        one read-modify-write is the one written, once."""
        if not self._file_lock_depth:
            raise RuntimeError("remote.json is written only under _state_file_lock")
        self._unpublished = _encoded_state(state)

    def _publish_state(
        self, pending: Replacement | None, unmade: OSError | None, body: bytes
    ) -> None:
        """Rename ``body`` over ``remote.json``, then know those bytes as this process's own.

        The token, the passphrase and the devices' digests, so written as
        core.credentials writes secrets: a temp of this write's own, created 0600
        and restricted to this account while still EMPTY (on NTFS, the DACL the
        rename carries over), then renamed over the target with the Windows
        contention retry. A shared ``remote.json.tmp`` written under the umask and
        chmodded afterwards held them 0644 until the chmod, and two writers
        collided on its name. Bytes, so the digest is of what is on disk: the next
        check finds these exact bytes and skips the parse, and a sibling process
        writing the same size in the same mtime tick is still seen, because its
        bytes differ. ``unmade`` is why there is no temp, raised now that there
        is something to write.
        """
        if pending is None:
            assert unmade is not None
            raise unmade
        pending.publish(body)
        with self._lock:
            self._disk = self._state_digest(body)
            self._disk_moves += 1
        if not pending.restricted and not self._said_unrestricted:
            self._said_unrestricted = True  # once: the flush rewrites the file every 30 s
            log.warning(_UNRESTRICTED, self._state_path, "the password and the link token")

    def _save_state(self) -> None:
        """Write the state in hand as it is (tests set fields, then save)."""
        with self._state_file_lock():
            self._write_state(self._state)

    # -- identity --

    @property
    def token(self) -> str:
        return self._state.token

    @property
    def password(self) -> str:
        self.reload_if_changed()
        return self._state.password

    @property
    def allow_write(self) -> bool:
        self.reload_if_changed()
        return self._state.allow_write

    def connection_info(self, port: int = DEFAULT_PORT) -> RemoteInfo:
        with self._lock:
            return RemoteInfo(self.token, self.password, build_local_url(self.token, port))

    def token_matches(self, supplied: str) -> bool:
        """Whether ``supplied`` is the whole link token, read fresh from the file first:
        ``regenerate-password --new-link`` in another shell retires the old link on the
        running server's very next request."""
        self.reload_if_changed()
        return _same(supplied, self.token)

    def password_matches(self, supplied: str) -> bool:
        """Whether ``supplied`` is the passphrase, as typed or as a phone types it.

        ``Amber River, cedar  DELTA`` matches ``amber-river-cedar-delta``: the
        normalized form is compared too (:func:`normalize_passphrase`). Both
        comparisons always run (a bitwise or, not ``or``), so the time taken says
        nothing about which one matched. Only the SUPPLIED side is normalized: a
        stored password that is not a word phrase (``Test1234``) still matches only
        itself, where normalizing both would let ``Test1235`` match it.
        """
        stored = self.password
        return _same(supplied, stored) | _same(normalize_passphrase(supplied), stored)

    def note_public_origin(self, origin: str | None) -> None:
        """Remember the public origin an authoritative source announced; ``None`` forgets it.

        Checked again here, so no caller can store an origin that would not pass.
        """
        checked = None if origin is None else check_public_origin(origin)
        with self._lock:
            self._public_origin = checked

    def remote_public_origin(self) -> str | None:
        """``https://<host>`` phones reach this server at, when an authoritative source said."""
        with self._lock:
            return self._public_origin

    def remote_json(self) -> dict[str, object]:
        """``GET /api/remote`` — the ONE place remote-specific state is exposed (§4-B)."""
        self.reload_if_changed()
        with self._lock:
            return {
                "allow_write": self._state.allow_write,
                "auto_off_at": self._state.auto_off_at,
                "version": __version__,
            }

    # -- switches --

    def set_allow_write(self, enabled: bool) -> None:
        with self._state_file_lock():
            self.reload_if_changed()
            self._state.allow_write = bool(enabled)
            self._write_state(self._state)

    def set_auto_off(self, at: datetime | None) -> None:
        """Record when Remote turns itself off, as ISO UTC with its offset.

        The TUI hands over its local time; a naive one is read as local
        (``astimezone``), so a phone in another timezone and a DST change both read
        the same instant (review of #243, auto-off published without an offset).
        """
        with self._state_file_lock():
            self.reload_if_changed()
            self._state.auto_off_at = None if at is None else _iso_seconds(at)
            self._write_state(self._state)

    def auto_off_deadline(self) -> datetime | None:
        """When Remote turns itself off, fresh from the file; ``None`` is never."""
        self.reload_if_changed()
        with self._lock:
            return _remote_instant(self._state.auto_off_at, naive_is_local=True)

    def auto_off_passed(self, now: datetime) -> bool:
        """Whether Remote's deadline is behind ``now``: then every request is a 404."""
        deadline = self.auto_off_deadline()
        return deadline is not None and now >= deadline

    def extend_auto_off(self, now: datetime) -> datetime | None:
        """Move the deadline :data:`AUTO_OFF_EXTEND` later; ``None`` when there is none.

        Never more than :data:`AUTO_OFF_CEILING` ahead of ``now``, and never EARLIER
        than it already was: a ``serve --auto-off 600`` deadline is already past the
        ceiling, and capping it there would turn "another hour" into nine fewer.
        """
        with self._state_file_lock():
            self.reload_if_changed()
            deadline = _remote_instant(self._state.auto_off_at, naive_is_local=True)
            if deadline is None:
                return None
            extended = max(
                deadline, min(max(deadline, now) + AUTO_OFF_EXTEND, now + AUTO_OFF_CEILING)
            )
            self._state.auto_off_at = _iso_seconds(extended)
            self._write_state(self._state)
            return extended

    def regenerate_password(self, *, new_link: bool = False) -> str:
        """A new password; every device is dropped with the old one (sockets close 4401).

        ``new_link`` also mints a new link token, so a link that leaked stops
        working everywhere: a running server reads it on its next request
        (:meth:`token_matches`). A new password also resets the failed-unlock budget.
        """
        with self._state_file_lock():
            UnlockBudget(self).clear_failed_unlocks()
            self._state.password = new_password()
            if new_link:
                self._state.token = new_token()
            for device in list(self._state.devices):
                self._drop(device.id, WS_CLOSE_UNAUTHORIZED)
            self._write_state(self._state)
            return self._state.password

    # -- devices --

    def _find_device(self, device_id: str) -> Device | None:
        return next((d for d in self._state.devices if d.id == device_id), None)

    def _device_by_secret(self, secret: str | None) -> Device | None:
        """The device whose cookie this is, by digest, whatever its state; compared in
        constant time against every device."""
        if not secret:
            return None
        self.reload_if_changed()
        digest = _secret_digest(secret).encode("ascii")
        found: Device | None = None
        with self._lock:
            for device in self._state.devices:
                if hmac.compare_digest(digest, device.secret_sha256.encode("utf-8", "replace")):
                    found = device
        return found

    def unlock_device(self, password: str, ua: str) -> tuple[str, Device] | None:
        """A new device and its cookie's secret when ``password`` is right, else ``None``.

        Compared before ``remote.json.lock`` is taken, so a wrong guess, the common
        case while someone is guessing, waits on no other process here; and compared
        again under the lock, since a ``regenerate-password`` may land in between.
        """
        import secrets

        if not self.password_matches(password):
            return None
        with self._state_file_lock():
            if not self.password_matches(password):
                return None
            now = _remote_now()
            secret = secrets.token_urlsafe(32)
            taken = {device.id for device in self._state.devices}
            device_id = f"dev_{secrets.token_hex(4)}"
            while device_id in taken:
                device_id = f"dev_{secrets.token_hex(4)}"
            device = Device(
                id=device_id,
                secret_sha256=_secret_digest(secret),
                ua=_audit_clean(ua, DEVICE_UA_MAX),
                first_seen=_iso_seconds(now),
                last_seen=_iso_seconds(now),
                expires_at=_iso_seconds(now + DEVICE_LIFETIME),
            )
            self._state.devices.append(device)
            self._write_state(self._state)
            return secret, device

    def device_for_cookie(self, secret: str | None) -> Device | None:
        """The SIGNED-IN device behind a cookie, its ``last_seen`` refreshed; ``None`` otherwise.

        Unknown, revoked, expired and signed out by idle (§2.4) are all ``None``, so
        the gate answers 401 and the page goes to unlock. ``last_seen`` is touched in
        memory; the flush writes it.
        """
        device = self._device_by_secret(secret)
        if device is None:
            return None
        now = _remote_now()
        with self._lock:
            if not device.device_signed_in(now):
                return None
            device.last_seen = _iso_seconds(now)
            return device

    def known_device_for_cookie(self, secret: str | None) -> Device | None:
        """The device a cookie belongs to while its lifetime lasts, signed in or signed out.

        What ``unlock`` asks first (SPEC §2.2 item 4): a phone that unlocked here
        before, idle past a day, re-unlocks into the same device, outside the
        global budget, with a 10-guess cap of its own.
        """
        device = self._device_by_secret(secret)
        if device is None or device.device_expired(_remote_now()):
            return None
        return device

    def reactivate_device(self, device_id: str, ua: str) -> tuple[str, Device] | None:
        """A known device's new cookie secret, after its owner typed the right passphrase.

        The id stays, so its push subscription carries on. The secret is new, the
        idle timer restarts and the wrong-guess count resets; ``expires_at`` does
        not move, so a cookie never outlives its device. ``None`` when the device is
        gone or expired.
        """
        import secrets

        now = _remote_now()
        with self._state_file_lock():
            self.reload_if_changed()
            device = self._find_device(device_id)
            if device is None or device.device_expired(now):
                return None
            secret = secrets.token_urlsafe(32)
            device.secret_sha256 = _secret_digest(secret)
            device.last_seen = _iso_seconds(now)
            device.failed_unlocks = 0
            if ua:
                device.ua = _audit_clean(ua, DEVICE_UA_MAX)
            self._write_state(self._state)
            return secret, device

    def known_device_failed(self, device_id: str) -> bool:
        """Count a wrong passphrase sent with this device's cookie; ``True`` when that
        was the :data:`KNOWN_DEVICE_FAILURES_MAX`-th and the device is revoked: a stolen
        cookie buys at most that many guesses outside the global budget."""
        with self._state_file_lock():
            self.reload_if_changed()
            device = self._find_device(device_id)
            if device is None:
                return False
            device.failed_unlocks += 1
            revoked = device.failed_unlocks >= KNOWN_DEVICE_FAILURES_MAX
            if revoked:
                self._drop(device_id, WS_CLOSE_UNAUTHORIZED)
            self._write_state(self._state)
            return revoked

    def device_is_live(self, device_id: str) -> bool:
        """Whether a socket's device may keep it open: there, signed in, not expired.

        An open socket is a device in use, so this touches ``last_seen`` as a request
        does; the stream asks every tick, by id, never by cookie.
        """
        self.reload_if_changed()
        now = _remote_now()
        with self._lock:
            device = self._find_device(device_id)
            if device is None or not device.device_signed_in(now):
                return False
            device.last_seen = _iso_seconds(now)
            return True

    def device_ids(self) -> list[str]:
        """Every current device's id, signed in or signed out; an expired one is not current."""
        self.reload_if_changed()
        now = _remote_now()
        with self._lock:
            return [d.id for d in self._state.devices if not d.device_expired(now)]

    def device_rows(self) -> list[dict[str, object]]:
        """Every current device as ``{id, ua, first_seen, last_seen, expires_at, signed_in}``."""
        self.reload_if_changed()
        now = _remote_now()
        with self._lock:
            return [d.device_row(now) for d in self._state.devices if not d.device_expired(now)]

    def _close_sockets(self, device_id: str, code: int) -> None:
        for close in self._closers.pop(device_id, set()):
            try:
                close(code)
            except Exception:  # a socket already gone must not stop the revoke
                log.debug("remote: closing a websocket on revoke failed", exc_info=True)

    def _drop(self, device_id: str, code: int) -> bool:
        before = len(self._state.devices)
        self._state.devices = [d for d in self._state.devices if d.id != device_id]
        self._close_sockets(device_id, code)
        return len(self._state.devices) != before

    def revoke_device(self, device_id: str) -> bool:
        """Remove one device and close its sockets with 4401; ``True`` if it existed."""
        with self._state_file_lock():
            self.reload_if_changed()
            dropped = self._drop(device_id, WS_CLOSE_UNAUTHORIZED)
            if dropped:
                self._write_state(self._state)
            return dropped

    def revoke_every_device(self, reason: str, close_code: int) -> int:
        """Remove every device, closing their sockets with ``close_code``; how many there were.

        4410 when Remote goes off (the switch, auto-off: the page says so and waits),
        4401 for ``asq remote revoke --all`` (the page goes to unlock).
        """
        with self._state_file_lock():
            self.reload_if_changed()
            revoked = [device.id for device in self._state.devices]
            for device_id in revoked:
                self._drop(device_id, close_code)
            self._write_state(self._state)
        log.info("remote: every device revoked (%s): %d", reason, len(revoked))
        return len(revoked)

    def prune_expired_devices(self, now: datetime) -> int:
        """Remove the devices past their lifetime (sockets close 4401); how many there were."""
        with self._state_file_lock():
            self.reload_if_changed()
            expired = [d.id for d in self._state.devices if d.device_expired(now)]
            for device_id in expired:
                self._drop(device_id, WS_CLOSE_UNAUTHORIZED)
            if expired:
                self._write_state(self._state)
            return len(expired)

    def flush_last_seen(self) -> None:
        """Persist ``last_seen`` and prune expired devices (called on a timer, not per request).

        Another process's change lands first: a flush that wrote memory over a
        fresher file would undo the very ``allow-write on`` this is about.
        """
        with self._state_file_lock():
            self.reload_if_changed()
            self.prune_expired_devices(_remote_now())
            self._write_state(self._state)

    def register_socket(self, device_id: str, close: Callable[[int], None]) -> None:
        with self._lock:
            self._closers.setdefault(device_id, set()).add(close)

    def unregister_socket(self, device_id: str, close: Callable[[int], None]) -> None:
        with self._lock:
            sockets = self._closers.get(device_id)
            if sockets:
                sockets.discard(close)
                if not sockets:
                    del self._closers[device_id]

    # -- audit --

    def audit(self, device_id: str, endpoint: str, summary: str) -> None:
        """``ts device_id endpoint summary`` — one line per write that went through (§4-E).

        Every field is passed through :func:`_audit_clean`: the log is
        line-oriented, and a caller-controlled newline (a note's ``kind`` once was
        one) let an unlocked device write a whole line of its choosing, attributed
        to another device. The log names devices by id, never by cookie, and is
        still owner-only before it holds a line: created empty at 0600, then
        restricted to this account (on NTFS, where the bits protect nothing, the
        DACL), the order ``core.atomic`` restricts a temp in.
        """
        fields = (
            _audit_clean(device_id, AUDIT_DEVICE_MAX),
            _audit_clean(endpoint, AUDIT_ENDPOINT_MAX),
            _audit_clean(summary, AUDIT_SUMMARY_MAX),
        )
        line = f"{_stamp()} {' '.join(fields)}\n".encode()
        with self._lock:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.close(os.open(self._audit_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            except FileExistsError:
                pass
            else:
                if not restrict_to_owner(self._audit_path):
                    log.warning(_UNRESTRICTED, self._audit_path, "what each device wrote")
            # Binary, so a line ends in "\n" on Windows too.
            with self._audit_path.open("ab") as handle:
                handle.write(line)


class UnlockBudget:
    """The failed-unlock budget across EVERY client (SPEC §2.2 item 3).

    The per-client limiter bounds one address; addresses are cheap. After
    :data:`UNLOCK_GLOBAL_FAILURES` wrong guesses inside
    :data:`UNLOCK_GLOBAL_WINDOW_SECONDS`, an unlock that is neither from the machine
    itself (:func:`is_direct_loopback`) nor carrying a known device's cookie is
    refused with 429 ``locked_out`` WITHOUT its passphrase being evaluated: at most
    960 guesses a day for anyone holding only the link. A right guess does not get
    through either (SPEC §9.1): if it did, a wrong one would still say "wrong" and
    the budget would bound nothing.

    The guesses are kept in ``remote.json`` (:attr:`_State.unlock_failures`), so
    ``asq remote status`` in another shell sees the same budget the server
    enforces, and a restart does not hand out another twenty. No header and no
    success resets it; a new password does.
    """

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime

    def _recent(self, now: datetime) -> list[datetime]:
        """The counted guesses inside the window, oldest first. Callers hold the lock."""
        start = now - timedelta(seconds=UNLOCK_GLOBAL_WINDOW_SECONDS)
        stamps = (_remote_instant(stamp) for stamp in self._runtime._state.unlock_failures)
        return sorted(stamp for stamp in stamps if stamp is not None and stamp > start)

    def unlock_budget_allows(self, direct: bool) -> bool:
        """Whether this unlock may be evaluated: always from the machine, else within budget."""
        return direct or self.budget_exhausted_until() is None

    def record_failed_unlock(self) -> bool:
        """Count one wrong guess; ``True`` when it is the one that trips the budget."""
        runtime = self._runtime
        now = _remote_now()
        with runtime._state_file_lock():
            runtime.reload_if_changed()
            recent = [*self._recent(now), now]
            runtime._state.unlock_failures = [_iso_seconds(stamp) for stamp in recent]
            runtime._write_state(runtime._state)
        return len(recent) == UNLOCK_GLOBAL_FAILURES

    def clear_failed_unlocks(self) -> None:
        """Forget every counted guess (a new password does this)."""
        runtime = self._runtime
        with runtime._state_file_lock():
            runtime.reload_if_changed()
            runtime._state.unlock_failures = []
            runtime._write_state(runtime._state)

    def budget_failures(self) -> int:
        """How many wrong guesses count against the budget right now."""
        self._runtime.reload_if_changed()
        with self._runtime._lock:
            return len(self._recent(_remote_now()))

    def budget_exhausted_until(self) -> datetime | None:
        """When unlocks open again, while the budget is spent; ``None`` while it is not.

        That is when the guess that keeps the count at the limit ages out: the
        oldest one, when exactly the limit was reached.
        """
        self._runtime.reload_if_changed()
        with self._runtime._lock:
            recent = self._recent(_remote_now())
        if len(recent) < UNLOCK_GLOBAL_FAILURES:
            return None
        return recent[-UNLOCK_GLOBAL_FAILURES] + timedelta(seconds=UNLOCK_GLOBAL_WINDOW_SECONDS)


# --- what the server reads and writes: the seams -----------------------------------


Snapshot = Callable[[], object]
ProjectSource = Callable[[str | None], object]
"""An optional project (id/name/codename) → that project's ``--json`` payload (``fleet ls``,
``board``, ``task list``, ``context list``). ``None`` is the CURRENT project, as it always
was. Raises :class:`NoSuchProject`."""
PaneSource = Callable[[str, str | None, int], dict[str, object]]
"""Agent label, optional project, scrollback lines → one pane capture.

``history`` of 0 is today's live-screen-only frame, byte for byte (§4-L).
Raises :class:`NoSuchAgent` / :class:`NoSuchProject`."""
TranscriptSource = Callable[[str, str | None, int, str | None, int | None], dict[str, object]]
"""Agent, optional project, limit, ``before`` cursor, width → one page of conversation (§4-M).

``width`` is the reader's column count; ``None`` wraps at the agent's own pane width.
A missing or unreadable transcript is an EMPTY page, never an error: an agent
that has not written one yet must still open in the page."""
ExplainabilitySource = Callable[[str, str | None], dict[str, object]]
"""Agent label, optional project → the §4-I card payload. Raises :class:`NoSuchAgent` or
:class:`NoSuchProject` only; never anything else."""
WriteHandler = Callable[[dict[str, Any]], tuple[dict[str, object], str]]
"""Body in → ``(result, audit summary)``; raise :class:`RequestError` to refuse."""
KitEndpoint = Callable[["Request", Device, dict[str, Any]], Awaitable["Response"]]
"""A lane route's endpoint (:meth:`RemoteKit.kit_route`): the request, the device the gate
found, and the parsed body (``{}`` for a GET)."""


def write_endpoint_names() -> tuple[str, ...]:
    """Every name ``POST api/{name}`` answers: the plan's seven, then the agent actions."""
    from aisquare.services import remote_actions

    return WRITE_ENDPOINTS + remote_actions.ACTION_ENDPOINTS


_agent_locks: dict[tuple[str, str], threading.Lock] = {}
_agent_locks_guard = threading.Lock()


def remote_agent_lock(project_id: str, label: str) -> threading.Lock:
    """The one lock for every action on one agent, process-wide.

    Callers take it without blocking and answer 409 ``busy`` when it is held: a
    second stop, restart or quick answer arriving while the first still runs
    would otherwise act on the state the first is in the middle of changing.
    """
    with _agent_locks_guard:
        return _agent_locks.setdefault((project_id, label), threading.Lock())


def check_remote_key_names(keys: object) -> list[str]:
    """``keys`` when every one is a key the pad sends; else 400 ``invalid_key`` (SPEC §2.1).

    The boundary for every key a phone sends, ``send-keys`` and quick answers
    alike. Keys used to go to tmux as they came, and tmux ends a command at any
    argument whose last character is ``;``: ``[";", "run-shell", "…"]`` ran a shell
    command and ``["Enter;", "kill-server"]`` killed every agent on the server. Each
    key must match :data:`REMOTE_KEY_NAME` in full; typed text never travels as keys
    (it is ``text``, sent as hex). More than :data:`SEND_KEYS_KEYS_MAX` keys is 413.
    The refusal names the key, scrubbed and cut to 32 characters, and the vocabulary.
    """
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        raise RequestError(400, "invalid_key", "'keys' must be a list of key names")
    if len(keys) > SEND_KEYS_KEYS_MAX:
        raise RequestError(
            413, "too_large", f"at most {SEND_KEYS_KEYS_MAX} keys at a time, not {len(keys)}"
        )
    for key in keys:
        if REMOTE_KEY_NAME.fullmatch(key) is None:
            raise RequestError(
                400,
                "invalid_key",
                f"'{_audit_clean(key, 32)}' is not a key the remote sends — "
                f"one of: {REMOTE_KEY_VOCABULARY}",
            )
    return list(keys)


def check_remote_text(text: str) -> None:
    """Refuse typed ``text`` holding a control character other than tab, newline and
    carriage return: 400 ``invalid``, naming the pad's key for it.

    Text reaches the pane as hex, byte for byte, so a control character in it IS a
    keystroke: ``"\\x03"`` was a Ctrl-C past the double-press guard, ``"\\x1a"`` the
    Ctrl-Z :data:`REMOTE_KEY_NAME` refuses as a key, and the audit line said
    ``text=1ch``, which cannot tell either from a letter. Keys go as ``keys``, where
    the allowlist, the guard and the trail see them by name.
    """
    found = _TEXT_CONTROL.search(text)
    if found is None:
        return
    char = found.group()
    key = _TEXT_CONTROL_KEYS.get(char)
    instead = f"send the pad's {key} key instead" if key else "no key of the pad sends it"
    raise RequestError(
        400, "invalid", f"'text' holds the control character U+{ord(char):04X} — {instead}"
    )


def check_project_add_root(raw: object) -> Path:
    """The project root ``project/add`` may register for ``raw``; else 400 ``invalid`` (§2.9).

    A registered project's board, tasks and memory are readable from every
    unlocked phone, and the write took any directory the machine's user could
    read: ``~/.ssh`` was one request away. So the path must be absolute (``~``
    allowed) and exist, and the project root it resolves to must be inside the
    home directory but not the home itself, below no hidden directory
    (``~/.ssh``, ``~/.aisquare``, ``~/.claude*``, ``~/.config``), and a project in
    fact: a git checkout, or a directory holding repos. Symlinks are resolved
    before any check, so a link into a hidden directory is judged where it points.
    """
    from aisquare.core.workspace import find_project_root
    from aisquare.services import fleet as fleet_service

    if not isinstance(raw, str) or not raw.strip():
        raise RequestError(400, "invalid", "'path' is required")
    if len(raw) > PROJECT_ADD_PATH_MAX:
        raise RequestError(413, "too_large", f"'path' is over {PROJECT_ADD_PATH_MAX} characters")
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        raise RequestError(400, "invalid", f"{path} is not an absolute path (start with / or ~)")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise RequestError(400, "invalid", f"{path} does not exist") from None
    if not resolved.is_dir():
        raise RequestError(400, "invalid", f"{resolved} is not a directory")
    root = find_project_root(resolved)
    home = Path.home().resolve()
    if root == home or not root.is_relative_to(home):
        where = "is your home directory" if root == home else "is outside your home directory"
        raise RequestError(400, "invalid", f"{root} {where}: add a project inside it")
    hidden = next((part for part in root.relative_to(home).parts if part.startswith(".")), None)
    if hidden is not None:
        raise RequestError(400, "invalid", f"{root} is inside the hidden directory {hidden}")
    if not (fleet_service.is_git_project(root) or _holds_repositories(root)):
        raise RequestError(
            400, "invalid", f"{root} is neither a git checkout nor a directory of repositories"
        )
    return root


def _holds_repositories(root: Path) -> bool:
    """Whether a direct child of ``root`` is a repository: the multi-repo project shape."""
    try:
        return any((child / ".git").exists() for child in root.iterdir() if child.is_dir())
    except OSError:
        return False


@dataclass(frozen=True)
class Sources:
    """The read-only JSON — by default the very functions ``asq --json`` prints."""

    projects: Snapshot
    fleet: ProjectSource
    board: ProjectSource
    tasks: ProjectSource
    memory: ProjectSource
    panes: PaneSource
    transcript: TranscriptSource = field(
        default=lambda label, project, limit, before, width: _live_transcript(
            label, project, limit, before, width
        )
    )
    explainability: ExplainabilitySource = field(
        default=lambda label, project: _live_explainability(label, project)
    )


@dataclass(frozen=True)
class Writes:
    """The write endpoints (§4-E) — by default the same services the CLI commands call."""

    handlers: dict[str, WriteHandler]


def _pane_payload(capture: Capture) -> dict[str, object]:
    """The four keys every pane response has carried since day one (§4-D), and
    ``cursor_visible``: Claude Code hides the terminal's cursor (``ESC[?25l``), and a
    page that drew it anyway showed a stray block wherever the hidden cursor rested."""
    return {
        "rows": capture.lines,
        "cursor": [capture.facts.cursor_x, capture.facts.cursor_y],
        "width": capture.facts.width,
        "height": capture.facts.height,
        "cursor_visible": capture.facts.cursor_visible,
    }


def _live_panes(label: str, project: str | None = None, history: int = 0) -> dict[str, object]:
    """One pane frame: the live screen, or scrollback and the screen together (§4-L).

    ``history`` of 0 takes the SAME call today took and returns the live keys
    alone (:func:`_pane_payload`), so the live stream and every existing client
    are untouched — the history keys appear only when history was asked for.
    """
    from aisquare.core.store import store_session
    from aisquare.services import fleet as fleet_service

    target = _resolve_project(project)
    with store_session() as store:
        agent = store.fleet_agent_by_label(target.id, label, live_only=True)
    if agent is None:
        raise NoSuchAgent(f"no live agent {label!r} in {target.root.name or target.id}")
    server = fleet_service.server_for(agent.tmux_socket)
    if history <= 0:
        return _pane_payload(server.capture(agent.pane_id))
    capture = server.capture_history(agent.pane_id, history=min(history, HISTORY_CAP))
    payload = _pane_payload(capture)
    payload["history_size"] = capture.facts.history_size
    payload["history"] = capture.scrollback
    if history > HISTORY_CAP:
        # Said out loud: without this a capped answer is indistinguishable from
        # a young pane, and the client would conclude there is nothing older.
        payload["history_capped"] = HISTORY_CAP
    return payload


def _live_transcript(
    label: str,
    project: str | None = None,
    limit: int = 0,
    before: str | None = None,
    width: int | None = None,
) -> dict[str, object]:
    """One page of the agent's own conversation, from the board's transcript (§4-M).

    The pane cannot answer this: agent panes are alternate-screen and tmux keeps
    no scrollback for them. The board already records where the transcript is.
    ``width`` is the phone's own column count; without it the lines wrap at the
    agent's pane width, which a phone narrower than the pane re-wraps into a mess.
    """
    from aisquare.core.store import store_session
    from aisquare.services import transcript as transcript_service

    target = _resolve_project(project)
    with store_session() as store:
        agent = store.fleet_agent_by_label(target.id, label, live_only=True)
        if agent is None:
            raise NoSuchAgent(f"no live agent {label!r} in {target.root.name or target.id}")
        session = store.get_session(agent.session_id) if agent.session_id else None
    path = session.transcript_path if session is not None else None
    page = transcript_service.read_page(
        path,
        limit=limit or transcript_service.DEFAULT_LIMIT,
        before=before,
        width=width or _pane_width(agent),
    )
    return page.page_json()


def _pane_width(agent: FleetAgent) -> int:
    """The agent's own pane width, so wrapped lines match the terminal they land in.

    Best effort by design: a dead pane, or a tmux that will not answer, costs a
    sensible 80 columns and never the page itself — the conversation is on disk
    and does not depend on the pane still being there.
    """
    from aisquare.services import fleet as fleet_service

    try:
        return fleet_service.server_for(agent.tmux_socket).capture(agent.pane_id).facts.width
    except Exception:
        return 80


def _agent_state_counts(agents: list[FleetAgentStatus]) -> dict[str, int]:
    """The Projects screen's per-project summary — one call's worth of ``fleet ls``, counted.

    PLAN §4-K: a flat object, the five CLI wire words always present, zeros
    included. The words are the CLI's own (§4-B keeps ``asq --json`` vocabulary
    verbatim) — the FE maps ``attention`` to "NEEDS YOU" for display; that
    spelling is never invented here.

    ``unknown`` is the sixth state a derived row can carry and §4-K does not name
    it, so it is appended ONLY when an agent is actually in it: dropping those
    rows would make the counts under-report a project's fleet, which on the
    Projects screen reads as "no agents" and is worse than an extra key the FE
    can ignore. Every agent is counted exactly once.
    """
    counts = {"working": 0, "waiting": 0, "attention": 0, "exited": 0, "lost": 0}
    for status in agents:
        counts[status.state] = counts.get(status.state, 0) + 1
    return counts


def remote_board_payload(project: str | None = None) -> dict[str, object]:
    """``GET api/board`` and the ``board`` frame — the ONE call into ``board_data``.

    The project's root as ``cwd`` is exactly what ``asq board --json`` prints when
    run there, ``AISQUARE_TEAM_HUB`` included (``team_service._project``); ``None``
    is the current project, as it always was.

    #240 fold: pass ``exclude_kinds=team_service.CAPTAIN_AUDIT_KINDS`` here (one line).
    """
    from aisquare.cli.team import board_json
    from aisquare.services import team as team_service

    cwd = None if project is None else _resolve_project(project).root
    return board_json(*team_service.board_data(cwd))


def live_sources() -> Sources:
    """The real thing: the ``--json`` builders over the live store and tmux."""

    def projects_payload() -> object:
        from aisquare.cli.common import projects_json
        from aisquare.core.store import store_session
        from aisquare.services import fleet as fleet_service
        from aisquare.services import project as project_service

        all_projects = project_service.list_projects()
        with store_session() as store:
            group_names = {g.id: g.name for g in store.project_groups()}
        rows = projects_json(all_projects, group_names=group_names)
        for row, one in zip(rows, all_projects, strict=True):
            agents = fleet_service.list_agents(one, live_only=True)
            row["agents"] = _agent_state_counts(agents)
        return rows

    def fleet_payload(project: str | None = None) -> object:
        from aisquare.cli.fleet import agents_json
        from aisquare.services import fleet as fleet_service

        target = _resolve_project(project)
        return agents_json(target, fleet_service.list_agents(target, live_only=True))

    def tasks_payload(project: str | None = None) -> object:
        from aisquare.services import team as team_service

        cwd = None if project is None else _resolve_project(project).root
        return [task.model_dump(mode="json") for task in team_service.list_tasks(None, cwd=cwd)]

    def memory_payload(project: str | None = None) -> object:
        from aisquare.core.store import store_session
        from aisquare.services import context as context_service

        if project is None:
            entries = context_service.list_entries()
        else:
            target = _resolve_project(project)
            with store_session() as store:
                entries = store.entries(project_id=target.id)
        return [entry.model_dump(mode="json") for entry in entries]

    return Sources(
        projects=projects_payload,
        fleet=fleet_payload,
        board=remote_board_payload,
        tasks=tasks_payload,
        memory=memory_payload,
        panes=_live_panes,
        transcript=_live_transcript,
        explainability=_live_explainability,
    )


# --- explainability card (§4-I) --------------------------------------------------------

DOCTOR_TTL_SECONDS = 30.0
"""How long one doctor verdict is reused: its proxy probe dials a socket."""


@dataclass(frozen=True)
class DoctorVerdict:
    """What the explainability doctor says, reduced to what the card needs."""

    sdk_present: bool
    red: list[str]
    """One line per failing check: ``name: detail``."""
    install_hint: str | None = None


def explainability_payload(
    *,
    agent: FleetAgent,
    session: TeamSession | None,
    turns: Sequence[TurnMetric],
    verdict: DoctorVerdict,
    policy: dict[str, object] | None,
) -> dict[str, object]:
    """The §4-I card, assembled from facts already in hand — never raises.

    ``available`` is true only when the SDK is present AND no doctor check is
    RED; otherwise ``reason`` says which. Model and tokens come from the board
    session and the recorded turns regardless, so the card still shows what the
    fleet knows on a machine where the SDK is missing. ``cost_estimate_usd`` is
    only ever set by the SDK lane — the CLI carries no price table, and a
    guessed figure on a demo card is worse than none.
    """
    payload: dict[str, object] = {"available": verdict.sdk_present and not verdict.red}
    if not verdict.sdk_present:
        hint = f" ({verdict.install_hint})" if verdict.install_hint else ""
        payload["reason"] = f"explainability SDK not installed{hint}"
    elif verdict.red:
        payload["reason"] = "doctor is RED: " + "; ".join(verdict.red)
    model = session.model if session is not None and session.model else None
    if model:
        payload["model"] = model
    tokens_in = [t.tokens_in for t in turns if t.tokens_in is not None]
    tokens_out = [t.tokens_out for t in turns if t.tokens_out is not None]
    if tokens_in:
        payload["tokens_in"] = sum(tokens_in)
    if tokens_out:
        payload["tokens_out"] = sum(tokens_out)
    if policy:
        payload["policy"] = policy
    stamps: list[datetime] = []
    if session is not None:
        stamps.append(session.last_seen_at)
    stamps.extend(t.ended_at or t.started_at for t in turns)
    stamps.append(agent.created_at)
    latest = max(stamps)
    payload["updated_at"] = latest.isoformat(timespec="seconds")
    return payload


_doctor_cache: tuple[float, DoctorVerdict] | None = None
_doctor_lock = threading.Lock()


def _doctor_verdict() -> DoctorVerdict:
    """The doctor's word on explainability, cached for :data:`DOCTOR_TTL_SECONDS`."""
    global _doctor_cache
    with _doctor_lock:
        if _doctor_cache is not None and time.monotonic() - _doctor_cache[0] < DOCTOR_TTL_SECONDS:
            return _doctor_cache[1]
        from aisquare.models import CheckStatus
        from aisquare.services import explainability as explainability_service
        from aisquare.services import explainability_ops as ops

        try:
            present = ops.sdk_presence().present
        except Exception:  # the doctor's own failure is a RED line, not a 500
            present = False
        try:
            red = [
                f"{check.name}: {check.detail}"
                for check in ops.checks()
                if check.status is CheckStatus.fail
            ]
        except Exception as exc:
            red = [f"doctor: {exc}"]
        hint = None if present else explainability_service.install_hint()
        verdict = DoctorVerdict(sdk_present=present, red=red, install_hint=hint)
        _doctor_cache = (time.monotonic(), verdict)
        return verdict


def _explainability_policy() -> dict[str, object] | None:
    """The policy applied to this machine's model traffic, from the config."""
    from aisquare.core.config import load_config
    from aisquare.services import explainability_ops as ops

    try:
        config = load_config()
        settings = config.explainability
        target = ops.resolve_target(settings, None)
    except Exception:
        return None
    return {
        "tracing": settings.enabled,
        "shipping": settings.ship,
        "target": target.name,
        "gateway": target.gateway_url or None,
        "redaction": ops.redaction_summary(config.redaction.level),
    }


def _live_explainability(label: str, project: str | None = None) -> dict[str, object]:
    """The card for one live agent of a project (the current one for ``None``).

    Only an unknown label or project raises (→ 404), never anything else.
    """
    from aisquare.core.store import store_session
    from aisquare.services import metrics as metrics_service

    target = _resolve_project(project)
    with store_session() as store:
        agent = store.fleet_agent_by_label(target.id, label, live_only=True)
        if agent is None:
            raise NoSuchAgent(f"no live agent {label!r} in {target.root.name or target.id}")
        session = store.get_session(agent.session_id) if agent.session_id else None
    turns: list[TurnMetric] = []
    if agent.session_id:
        try:
            turns = metrics_service.recent(project_id=target.id, session_id=agent.session_id)
        except Exception as exc:
            log.debug("remote: turn metrics for %s unavailable: %s", label, exc)
    return explainability_payload(
        agent=agent,
        session=session,
        turns=turns,
        verdict=_doctor_verdict(),
        policy=_explainability_policy(),
    )


def _required(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RequestError(400, "invalid", f"{key!r} is required")
    return value.strip()


_UNSAFE_IN_A_KEY_NAME = re.compile(r"[^A-Za-z0-9_-]")


def _audit_keys(keys: list[str] | None) -> str:
    """The key NAMES for a send-keys audit line — ``[Escape]``, ``[C-c]``, or ``0``.

    The count alone (``keys=1``) could not tell a Ctrl-C sent to a live agent
    from an arrow key, which is exactly the difference a write trail exists to
    record. Key names are a bounded tmux vocabulary, so recording them is safe;
    the typed TEXT stays a length only (``text=12ch``) because it is unbounded
    user content — counted, never captured (PLAN §4-E).

    Each name is scrubbed to the characters a tmux key name can actually hold.
    ``remote-audit.log`` is line-oriented (``ts device endpoint summary``), so a
    name carrying a newline would let an authenticated device forge an audit line —
    and an authenticated device is precisely who the trail exists to hold to
    account. Defence in depth now: :func:`check_remote_key_names` refuses any such
    name before a key is sent, and :meth:`Runtime.audit` scrubs every field.
    """
    if not keys:
        return "0"
    scrubbed = [_UNSAFE_IN_A_KEY_NAME.sub("?", key)[:32] or "?" for key in keys]
    return "[" + ",".join(scrubbed) + "]"


def _optional_ref(body: dict[str, Any], key: str) -> str | None:
    """An optional NAME or reference — blank and whitespace-only both mean absent.

    Correct for a project ref, a note's task or a role. WRONG for literal text a
    human typed: see :func:`_literal`.
    """
    value = body.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _literal(body: dict[str, Any], key: str) -> str | None:
    """Text to deliver verbatim — whitespace is CONTENT here, not emptiness.

    ``_optional_ref`` answers "did they name something", and a name that is all
    spaces is no name. A keystroke that is all spaces is a keystroke. Reading
    typed text with ``_optional_ref`` is what silently ate the space bar: a flush of
    ``" "`` became ``None``, so a write carrying only a space delivered nothing
    while the endpoint answered 200 ``sent: true``, and the audit line recorded
    ``text=0ch`` — the trail honestly reporting that no text was sent, the loss
    having happened before it. Absent or non-string is still absent; ``""`` is
    still nothing to send.
    """
    value = body.get(key)
    return value if isinstance(value, str) else None


DOUBLE_PRESS = (
    "a second Ctrl-C or Ctrl-D within 3 s exits Claude Code — send confirm_exit: true to mean it"
)


class _ExitKeyGuard:
    """When each agent was last sent Ctrl-C or Ctrl-D (SPEC §2.1, part 2).

    Claude Code exits on a second Ctrl-C ("Press Ctrl-C again to exit") and on
    Ctrl-D at an empty prompt, and an exit ends the row, releases its claims and
    wakes the manager: two taps on a phone that lagged, or two in one body, are
    refused unless the body says ``confirm_exit: true``. Per ``(project id,
    label)``, whichever phone sent the first.
    """

    def __init__(self) -> None:
        self._sent: dict[tuple[str, str], datetime] = {}
        self._lock = threading.Lock()

    def exit_keys_allowed(self, agent: tuple[str, str], exits: int, *, confirmed: bool) -> bool:
        """Note ``exits`` exit keys about to go to ``agent``; ``False``, noting nothing, when
        they would be a second press and are not ``confirmed``."""
        now = _remote_now()
        with self._lock:
            last = self._sent.get(agent)
            recent = last is not None and (now - last).total_seconds() < EXIT_KEY_REPEAT_SECONDS
            if not confirmed and (exits > 1 or recent):
                return False
            self._sent[agent] = now
            return True


def live_writes() -> Writes:
    """The write endpoints over the services the CLI commands call, then the agent actions."""
    from aisquare.services import remote_actions

    exit_keys = _ExitKeyGuard()

    def task_claim(body: dict[str, Any]) -> tuple[dict[str, object], str]:
        from aisquare.services import team as team_service

        author = _optional_ref(body, "as")
        task = team_service.claim_task(_required(body, "ref"), session_ref=author)
        return {"task": task.model_dump(mode="json")}, f"claimed {task.id} as={author or '-'}"

    def task_done(body: dict[str, Any]) -> tuple[dict[str, object], str]:
        from aisquare.services import team as team_service

        author = _optional_ref(body, "as")
        task = team_service.finish_task(
            _required(body, "ref"), note=_optional_ref(body, "note"), session_ref=author
        )
        return {"task": task.model_dump(mode="json")}, f"done {task.id} as={author or '-'}"

    def write_note(body: dict[str, Any]) -> tuple[dict[str, object], str]:
        """A note on a project's board: ``project``'s, or the current one's without it.

        The board resolves from the project's root exactly as ``asq note`` run
        there would; with ``as``, the session's own board still wins (the CLI's
        rule), so a note posted as an agent lands where that agent reads. Only
        the human's kinds (:data:`NOTE_KINDS`): ``kind`` went to the board and the
        audit line as it came, so a phone could forge the fleet's own reports and,
        with a newline in it, a line of the audit trail. The summary records who
        the note claims to be from (``as=``) and who it is for (``to=``).
        """
        from aisquare.services import team as team_service

        project = _optional_ref(body, "project")
        text = _required(body, "text")
        if len(text) > NOTE_TEXT_MAX:
            raise RequestError(413, "too_large", f"a note is at most {NOTE_TEXT_MAX} characters")
        kind = _optional_ref(body, "kind") or "note"
        if kind not in NOTE_KINDS:
            kinds = ", ".join(sorted(NOTE_KINDS))
            raise RequestError(400, "invalid", f"'kind' must be one of {kinds}")
        author, to = _optional_ref(body, "as"), _optional_ref(body, "to")
        event = team_service.add_note(
            text,
            session_ref=author,
            task_ref=_optional_ref(body, "task"),
            to_role=to,
            kind=kind,
            cwd=None if project is None else _resolve_project(project).root,
        )
        summary = f"{event.kind} seq={event.seq} to={to or '-'} as={author or '-'}"
        return {"event": event.as_envelope().model_dump(mode="json")}, summary

    def project_switch(body: dict[str, Any]) -> tuple[dict[str, object], str]:
        from aisquare.services import project as project_service

        name = _required(body, "name")
        try:
            project = project_service.switch(name)
        except KeyError:
            raise RequestError(404, "not_found", f"no project matches {name!r}") from None
        except ValueError as exc:
            raise RequestError(400, "ambiguous_project", str(exc)) from None
        return {"project": project.model_dump(mode="json")}, f"switched to {project.id}"

    def project_add(body: dict[str, Any]) -> tuple[dict[str, object], str]:
        """Register a project the phone names, within :func:`check_project_add_root`'s limits;
        ``added`` is false when it was registered already."""
        from aisquare.core.store import store_session
        from aisquare.core.workspace import project_id_for
        from aisquare.models import ProjectInfo

        root = check_project_add_root(body.get("path"))
        project = ProjectInfo(id=project_id_for(root), root=root, linked_repos=[])
        with store_session() as store:
            added = store.get_project(project.id) is None
            store.ensure_project(project)
        payload = {"project": project.model_dump(mode="json"), "added": added}
        return payload, f"added {project.id} {root}"

    def project_remove(body: dict[str, Any]) -> tuple[dict[str, object], str]:
        from aisquare.services import project as project_service

        ref = _required(body, "ref")
        try:
            report = project_service.forget(ref, purge=False)
        except KeyError:
            raise RequestError(404, "not_found", f"no project matches {ref!r}") from None
        except ValueError as exc:
            raise RequestError(400, "ambiguous_project", str(exc)) from None
        return {"report": _as_json(report)}, f"removed {ref}"

    def write_send_keys(body: dict[str, Any]) -> tuple[dict[str, object], str]:
        """Type into one agent's pane: ``text`` (as hex, nothing parses it), or pad ``keys``.

        Everything is checked before anything is sent: the keys against the
        allowlist, the caps, no control character in the text, one input per body
        (``text`` went first, so "Esc, then type" arrived as "type, then Esc"), and
        the double Ctrl-C. Once a byte may have reached the pane, a failure is
        still audited: the trail exists for what a device did to a live agent,
        finished or not.
        """
        from aisquare.core.store import store_session
        from aisquare.services import fleet as fleet_service

        label = _required(body, "agent")
        text = _literal(body, "text")
        keys = [] if body.get("keys") is None else check_remote_key_names(body["keys"])
        enter = bool(body.get("enter", False))
        if text and len(text) > SEND_KEYS_TEXT_MAX:
            raise RequestError(
                413,
                "too_large",
                f"'text' is at most {SEND_KEYS_TEXT_MAX} characters — longer goes as a tell",
            )
        if text:
            check_remote_text(text)
        if text and keys:
            raise RequestError(
                400, "text_and_keys", "send 'text' or 'keys', not both: they would arrive in turn"
            )
        if not text and not keys and not enter:
            raise RequestError(400, "invalid", "give 'text', 'keys' or 'enter'")
        target = _resolve_project(_optional_ref(body, "project"))
        with store_session() as store:
            agent = store.fleet_agent_by_label(target.id, label, live_only=True)
        if agent is None:
            raise NoSuchAgent(f"no live agent {label!r} in {target.root.name or target.id}")
        exits = sum(key in EXIT_KEYS for key in keys)
        confirmed = body.get("confirm_exit") is True
        if exits and not exit_keys.exit_keys_allowed(
            (target.id, label), exits, confirmed=confirmed
        ):
            raise RequestError(409, "double_press", DOUBLE_PRESS)
        summary = (
            f"{label}@{target.id} text={len(text or '')}ch keys={_audit_keys(keys)} enter={enter}"
        )
        server = fleet_service.server_for(agent.tmux_socket)
        try:
            if text:
                server.send_literal(agent.pane_id, text)
            if keys:
                server.send_keys(agent.pane_id, *keys)
            if enter:
                server.send_keys(agent.pane_id, "Enter")
        except Exception as exc:
            log.warning("remote: send-keys to %s failed: %s", label, exc)
            raise RequestError(
                400, "write_failed", f"{label}: {exc}", audit=f"{summary} failed"
            ) from exc
        return {"agent": label, "project": target.id, "sent": True}, summary

    return Writes(
        {
            "task/claim": task_claim,
            "task/done": task_done,
            "note": write_note,
            "project/switch": project_switch,
            "project/add": project_add,
            "project/remove": project_remove,
            "send-keys": write_send_keys,
            **remote_actions.action_handlers(),
        }
    )


def _as_json(value: object) -> object:
    """A pydantic model, a dataclass-ish object or a plain value as JSON data."""
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    if hasattr(value, "__dict__"):
        return {k: _as_json(v) for k, v in vars(value).items() if not k.startswith("_")}
    if isinstance(value, list | tuple):
        return [_as_json(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


# --- the app -----------------------------------------------------------------------


class _RateLimiter:
    """``UNLOCK_LIMIT`` attempts per ``UNLOCK_WINDOW_SECONDS`` per client, then 429.

    The client is uvicorn's resolved peer (:func:`_client_of`), never a header the
    sender writes. A client whose window has emptied is forgotten on the next
    attempt by anyone, so the table holds the addresses of the last minute and no
    more: keyed on a header, a fresh invented address per request grew it forever.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._attempts: dict[str, deque[float]] = {}

    def limiter_retry_after(self, client: str) -> float | None:
        """Count an attempt by ``client``: ``None`` when it may go ahead, else the seconds
        until it may (and nothing is counted)."""
        now = self._clock()
        for known, window in list(self._attempts.items()):
            while window and now - window[0] >= UNLOCK_WINDOW_SECONDS:
                window.popleft()
            if not window:
                del self._attempts[known]
        window = self._attempts.setdefault(client, deque())
        if len(window) >= UNLOCK_LIMIT:
            return UNLOCK_WINDOW_SECONDS - (now - window[0])
        window.append(now)
        return None


class _Cache:
    """One snapshot per kind per tick, however many sockets are open.

    It holds only what was asked for within the last tick. A kind carries the
    ``?project=`` ref as it was written, and every spelling that resolves is a
    kind of its own (an id prefix of any length, a name, a codename, and the id
    with any run of ``*``, ``?`` or ``[``, which the store's glob drops). Kept
    until they were asked for again, they grew the heap by a full payload per
    spelling for anyone unlocked, read-only included, until the process died.
    So each store first drops what has expired, and at most
    :data:`CACHE_KINDS_MAX` kinds are kept, the oldest going first.
    """

    def __init__(self, ttl: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._values: dict[str, tuple[float, object]] = {}

    def cached_snapshot(self, kind: str, compute: Snapshot) -> object:
        with self._lock:
            hit = self._values.get(kind)
            if hit is not None and self._clock() - hit[0] < self._ttl:
                return hit[1]
            value = compute()
            now = self._clock()
            for stale in [k for k, (at, _value) in self._values.items() if now - at >= self._ttl]:
                del self._values[stale]
            self._values.pop(kind, None)  # stored anew, so the dict stays oldest first
            self._values[kind] = (now, value)
            while len(self._values) > CACHE_KINDS_MAX:
                del self._values[next(iter(self._values))]
            return value


def _client_of(scope: Any) -> str:
    """The client's address as uvicorn resolved it; never a header the sender writes.

    ``_Server`` and ``run_foreground`` run uvicorn with ``proxy_headers`` trusting
    ``127.0.0.1`` alone, so behind ngrok (which connects from there) the peer is the
    rightmost ``X-Forwarded-For`` entry that is not ours: the address ngrok appended.
    Read here, the LEFTMOST entry gave a client a fresh identity per request.
    """
    client = scope.get("client")
    return str(client[0]) if client else "unknown"


def _forwarded_https(request: Request) -> bool:
    """Whether the hop to the browser is TLS: the scheme uvicorn resolved, which applies
    ngrok's ``X-Forwarded-Proto`` from the trusted 127.0.0.1 hop and from no one else."""
    return request.scope.get("scheme") in ("https", "wss")


_FORWARDING_HEADERS = frozenset(
    {b"x-forwarded-for", b"x-forwarded-proto", b"x-forwarded-host", b"forwarded"}
)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})


def _scope_header(scope: Any, wanted: bytes) -> str | None:
    """The first value of one request header, lower-cased; ``None`` when absent."""
    for name, value in scope.get("headers") or []:
        if name == wanted:
            return bytes(value).decode("latin-1").lower()
    return None


def is_direct_loopback(scope: Any) -> bool:
    """Whether a request comes from this machine's own browser, not through a tunnel.

    The peer is ``127.0.0.1`` or ``::1``, no forwarding header is present (ngrok
    always adds ``X-Forwarded-For``) and ``Host`` names the loopback. Such an unlock
    skips the global failed-unlock budget and its failures do not count toward it,
    so the owner at the machine can always unlock; the per-client limiter still
    applies. A request forwarded by ngrok is never direct.
    """
    client = scope.get("client")
    if not client or client[0] not in ("127.0.0.1", "::1"):
        return False
    if any(name in _FORWARDING_HEADERS for name, _value in scope.get("headers") or []):
        return False
    host = _scope_header(scope, b"host") or ""
    name = host[: host.find("]") + 1] if host.startswith("[") else host.partition(":")[0]
    return name in _LOOPBACK_HOSTS


def allowed_origin(scope: Any) -> str:
    """The one ``Origin`` a write or a socket may carry: the request's own ``scheme://host``.

    ``host`` is the ``Host`` header as sent, port included; ``X-Forwarded-Host`` is
    never read (ngrok preserves ``Host``, and that header only widens the set behind
    some other hop). ``scheme`` is the one uvicorn resolved: ngrok's
    ``X-Forwarded-Proto`` applied from the trusted 127.0.0.1 hop alone.
    """
    scheme = "https" if scope.get("scheme") in ("https", "wss") else "http"
    return f"{scheme}://{_scope_header(scope, b'host') or ''}"


def _history_param(raw: str | None) -> int:
    """``?history=`` as a line count; raises ``ValueError`` for anything else.

    Absent and empty both mean 0 — today's live-only frame. A negative or
    non-numeric value is refused rather than clamped, because silently reading
    ``history=-5`` or ``history=lots`` as "no history" would return a live-only
    frame to a client that believes it asked for scrollback, and the page would
    render an empty conversation with nothing to say why.
    """
    if raw is None or raw == "":
        return 0
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"'history' must be a whole number of lines, not {raw!r}") from None
    if value < 0:
        raise ValueError(f"'history' cannot be negative, got {value}")
    return value


def _limit_param(raw: str | None) -> int:
    """``?limit=`` as a turn count; 0 means "the default". Refuses nonsense.

    Capped in :mod:`aisquare.services.transcript`, not here — the cap belongs
    with the reader that has to honour it.
    """
    if raw is None or raw == "":
        return 0
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"'limit' must be a whole number of turns, not {raw!r}") from None
    if value < 0:
        raise ValueError(f"'limit' cannot be negative, got {value}")
    return value


TRANSCRIPT_WIDTH_MIN = 20
TRANSCRIPT_WIDTH_MAX = 200


def _width_param(raw: str | None) -> int | None:
    """``?width=`` as the reader's column count; ``None`` (absent) is the pane's own width.

    Refused outside 20-200 rather than clamped: a page that measured its own
    columns wrongly should hear so, not get lines wrapped for a screen it is not.
    """
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"'width' must be a whole number of columns, not {raw!r}") from None
    if not TRANSCRIPT_WIDTH_MIN <= value <= TRANSCRIPT_WIDTH_MAX:
        raise ValueError(
            f"'width' must be {TRANSCRIPT_WIDTH_MIN} to {TRANSCRIPT_WIDTH_MAX} columns, not {value}"
        )
    return value


def _is_navigation(rel: str, accept: str) -> bool:
    """Whether this is a page navigation, which is the ONLY thing the SPA fallback serves.

    A missing file must not come back as the document. ``GET /assets/index-<old
    hash>.js`` used to return 200 with the body of ``index.html``; the browser
    then refuses to execute HTML as JavaScript and the app fails to boot with no
    honest error anywhere — the server having answered 200 to everything.

    ``assets/`` is excluded outright: those names are content-addressed, so a
    miss is a genuine miss and never a route. Otherwise a path with no file
    extension is a route (``/fleet/coder-1``, ``/unlock``), and a path that has
    one is a file request unless the client actually asked for a document.
    """
    if rel.startswith("assets/") or rel.startswith("/assets/"):
        return False
    if not PurePosixPath(rel).suffix:
        return True
    return "text/html" in accept


def _cache_control(rel: str) -> str:
    """How long the browser may keep this file without asking again."""
    name = PurePosixPath(rel).name
    if not rel.startswith("assets/"):
        return INDEX_CACHE_CONTROL
    stem = PurePosixPath(name).stem
    return ASSET_CACHE_CONTROL if _HASHED_ASSET.search(stem) else MUTABLE_CACHE_CONTROL


def _error_body(error: str, message: str | None = None) -> dict[str, object]:
    """``{error, message}``, the one shape of every refusal (SPEC §0.5)."""
    body: dict[str, object] = {"error": error}
    if message:
        body["message"] = message
    return body


def _json_error(status: int, error: str, message: str | None = None) -> Response:
    from starlette.responses import JSONResponse

    return JSONResponse(_error_body(error, message), status_code=status)


# --- the choke point: five gates in front of every route (SPEC §1.2) --------------------


def remote_gate_token(runtime: Runtime, scope: Any) -> bool:
    """Gate 1: the path is under ``/r/<the token>/``, the whole token, in constant time."""
    path = str(scope.get("path", ""))
    if not path.startswith("/r/"):
        return False
    supplied = path[3:].split("/", 1)[0]
    return bool(supplied) and runtime.token_matches(supplied)


def remote_gate_auto_off(runtime: Runtime, scope: Any) -> bool:
    """Gate 2: Remote's auto-off deadline has not passed (SPEC §2.5).

    Once it has, every request is answered exactly as a wrong token is, for the
    TUI's Remote and ``asq remote serve`` alike, whether or not whatever turns the
    server off has run yet: the deadline is the server's to keep.
    """
    return not runtime.auto_off_passed(_remote_now())


def remote_gate_origin(scope: Any) -> bool:
    """Gate 3: a write or a socket comes from the remote page's own origin (SPEC §2.8).

    Asked for every method but GET and HEAD, and for every handshake. ``Origin``
    must be there, once, and equal :func:`allowed_origin`; ``null`` (an opaque
    origin) never does. ``SameSite=Lax`` already keeps the cookie off a sibling
    tunnel's POST, ngrok's domains being on the Public Suffix List; this holds
    for a browser whose list is stale, and for anything that sends the cookie anyway.
    """
    origins = [value for name, value in scope.get("headers") or [] if name == b"origin"]
    return len(origins) == 1 and _scope_header(scope, b"origin") == allowed_origin(scope)


def remote_gate_device(runtime: Runtime, scope: Any) -> Device | None:
    """Gate 4: the unlocked device behind the request's ``asq_remote`` cookie, or ``None``."""
    from starlette.requests import HTTPConnection

    secret = HTTPConnection(scope).cookies.get(COOKIE)
    return runtime.device_for_cookie(secret) if secret else None


async def remote_gate_body(scope: Any, receive: Any) -> Any | None:
    """Gate 5: the whole body, read once against :data:`MAX_BODY_BYTES`, then replayed.

    ``None`` means too large, and the caller answers 413: a declared
    ``Content-Length`` over the cap before a byte is read, a chunked body as soon
    as the read passes it (the read is cut at cap + 1). Otherwise the ``receive``
    returned hands the app the body as one ``http.request`` message and then
    defers to the server's own, so a disconnect still arrives as a disconnect.
    """
    for name, value in scope.get("headers") or []:
        if name == b"content-length":
            with contextlib.suppress(ValueError):  # malformed: the capped read still bounds it
                if int(value) > MAX_BODY_BYTES:
                    return None
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            first = message  # the client left mid-body: the app sees exactly that
            break
        chunk = bytes(message.get("body", b""))
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
        if not message.get("more_body", False):
            first = {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            break
    replayed = False

    async def receive_replayed() -> Any:
        nonlocal replayed
        if replayed:
            return await receive()
        replayed = True
        return first

    return receive_replayed


LOCKED_OUT = (
    "too many wrong passwords in the last 30 min — new unlocks are paused; a phone that was "
    "unlocked here before can still unlock. On the machine: "
    "aisquare remote regenerate-password --new-link"
)
LOCKOUT_ALERT = (
    "New unlocks are paused for 30 min. If that is not you, run "
    "`aisquare remote regenerate-password --new-link` at the machine."
)


def _unlock_lockout_alert(runtime: Runtime) -> None:
    """Say, once per trip, that the failed-unlock budget is spent (SPEC §2.2 item 8).

    A warning in the server's log, and a push to every phone that has notifications
    on: someone holding the link is guessing, and the owner can rotate it. The
    status line of ``asq remote status`` and the modal read the budget themselves.
    """
    from aisquare.services import remote_push

    until = UnlockBudget(runtime).budget_exhausted_until()
    log.warning(
        "remote: %d wrong passwords in 30 min — new unlocks are paused until %s; if that is "
        "not you, rotate the link: aisquare remote regenerate-password --new-link",
        UNLOCK_GLOBAL_FAILURES,
        "?" if until is None else _iso_seconds(until),
    )
    try:
        remote_push.push_security_alert(runtime.device_ids(), LOCKOUT_ALERT)
    except Exception:  # the alert is a courtesy: the lockout itself already holds
        log.warning("remote: the lockout alert could not be queued", exc_info=True)


def _route_path(scope: Any) -> str:
    """The path inside ``/r/<token>``, which is what the routes are written against."""
    return "/" + str(scope.get("path", "")).removeprefix("/r/").partition("/")[2]


def _needs_a_device(scope: Any) -> bool:
    """Gate 4's reach: every socket, and every ``/api/*`` path but ``POST /api/unlock``.

    The page needs no device, or the unlock screen could not load. Unlock reads
    the cookie itself, to tell a phone that unlocked here before from a stranger.
    """
    if scope["type"] == "websocket":
        return True
    path = _route_path(scope)
    return path.startswith("/api/") and not (
        scope.get("method") == "POST" and path == "/api/unlock"
    )


async def _refuse_at_the_gate(
    scope: Any,
    receive: Any,
    send: Any,
    status: int,
    error: str,
    message: str | None,
    close_code: int,
) -> None:
    """JSON over HTTP; on a handshake, a denial response where the server can send one
    and otherwise the close code the page maps (SPEC §1.6)."""
    if scope["type"] == "websocket" and "websocket.http.response" not in scope.get(
        "extensions", {}
    ):
        await send({"type": "websocket.close", "code": close_code})
        return
    await _json_error(status, error, message)(scope, receive, send)


class _TokenGate:
    """Pure ASGI in front of every route: the five gates of SPEC §1.2, in order.

    Every HTTP request and every WebSocket handshake passes all five before any
    route sees it, built-in and lane routes alike, so no route can forget one:
    the token (404, or close 4404), auto-off (the same 404), the origin of a
    write or a socket (403 ``bad_origin``, or 4403), the device behind the
    cookie for every ``/api`` path but unlock and for every socket (401, or
    4401), and the body cap for anything that may carry a body (413). The
    device lands in the scope; no route reads the cookie again.
    """

    def __init__(self, app: Any, runtime: Runtime, kit: RemoteKit) -> None:
        self._app = app
        self._runtime = runtime
        self.kit = kit
        """The app's :class:`RemoteKit`, which tests reach through here."""

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        kind = scope.get("type")
        if kind not in ("http", "websocket"):  # lifespan: the lanes start and stop with the app
            await self._app(scope, receive, send)
            return
        runtime = self._runtime
        if not (remote_gate_token(runtime, scope) and remote_gate_auto_off(runtime, scope)):
            await _refuse_at_the_gate(
                scope, receive, send, 404, "not_found", None, WS_CLOSE_NOT_FOUND
            )
            return
        method = scope.get("method")  # a handshake has none, and is always asked
        if method not in ("GET", "HEAD") and not remote_gate_origin(scope):
            await _refuse_at_the_gate(
                scope,
                receive,
                send,
                403,
                "bad_origin",
                "this request did not come from the remote page",
                WS_CLOSE_BAD_ORIGIN,
            )
            return
        if _needs_a_device(scope):
            device = remote_gate_device(runtime, scope)
            if device is None:
                await _refuse_at_the_gate(
                    scope, receive, send, 401, "unauthorized", None, WS_CLOSE_UNAUTHORIZED
                )
                return
            scope[DEVICE_SCOPE] = device
        if kind == "http" and method not in ("GET", "HEAD", "OPTIONS"):
            replayed = await remote_gate_body(scope, receive)
            if replayed is None:
                too_large = f"the body is over {MAX_BODY_BYTES} bytes"
                await _json_error(413, "too_large", too_large)(scope, receive, send)
                return
            receive = replayed
        await self._app(scope, receive, send)


# --- the kit: what every route of one app shares ---------------------------------------


IN_PROGRESS = "a request with this request_id is still running — its answer will follow"
"""409 ``in_progress``: a retry that arrived while the first try was still running."""


def _new_action_ledger() -> ActionLedger:
    from aisquare.services import remote_actions

    return remote_actions.new_action_ledger()


def _ledger_request_id(body: dict[str, Any]) -> str | None:
    """Take a write's optional ``request_id`` out of its body; 400 when it is malformed."""
    request_id = body.pop("request_id", None)
    if request_id is None:
        return None
    if not isinstance(request_id, str) or REQUEST_ID.fullmatch(request_id) is None:
        raise RequestError(
            400, "invalid", "'request_id' must be 1 to 64 letters, digits, '_' or '-'"
        )
    return request_id


def _ledger_body(response: Response) -> dict[str, object]:
    """What the ledger keeps of a response: its JSON object, else ``{}``."""
    try:
        payload = json.loads(bytes(response.body))
    except (AttributeError, ValueError):  # a streamed response has no body to keep
        return {}
    return payload if isinstance(payload, dict) else {}


@dataclass(eq=False)
class RemoteKit:
    """What every route of one app shares (SPEC §1): the runtime, the seams, the lanes' state.

    One per :func:`build_app`. Lane modules get it from their route factories
    (``needs_routes(kit)``, ``push_routes(kit)``, ``action_routes(kit)``) and the
    lifespan (``start_needs_watch(kit)``, ``start_push_sender(kit)``), and keep
    their live objects in :attr:`lane_state`: ``"needs"`` is the needs watcher,
    ``"push"`` the push sender. Everything a route needs to answer the way every
    other route answers is a ``kit_`` method here: the device the gate found,
    the one body parser, the refusal shape, the audit line, the write gate.
    """

    runtime: Runtime
    tick: float = TICK_SECONDS
    needs_listeners: list[Callable[[list[NeedsItem], datetime], None]] = field(default_factory=list)
    """Called after every needs scan with ``(all items, scanned_at)``."""
    ledger: ActionLedger = field(default_factory=_new_action_ledger)
    """The request ledger every write-gated request passes (SPEC §1.5)."""
    pane_pool: ThreadPoolExecutor | None = None
    """Made on the first pane capture (:meth:`kit_pane_pool`); the lifespan shuts it down."""
    sockets: dict[str, list[Callable[[int], None]]] = field(default_factory=dict)
    """Each device's live sockets, oldest first, as closers that take a close code."""
    lane_state: dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def kit_device(self, request: HTTPConnection) -> Device:
        """The device gate 4 found for this request; the cookie is never looked up twice."""
        device = request.scope.get(DEVICE_SCOPE)
        if not isinstance(device, Device):
            # Only a route outside the gate's reach can get here: refuse, never guess.
            raise RequestError(401, "unauthorized", "no unlocked device for this request")
        return device

    async def kit_json_object(self, request: Request) -> dict[str, Any]:
        """THE body parser: the JSON object the request carries, ``{}`` for none at all.

        No other code in a remote module reads a body (``tests/test_remote_gates.py``
        pins it), so every route refuses a malformed one the same way: 400 ``invalid``.
        That includes a body nested deeper than ``json`` recurses into: it raises
        ``RecursionError``, not ``ValueError`` (from about 1 000 levels on 3.11), and
        anyone holding only the URL can post one to ``unlock``.
        """
        from starlette.requests import ClientDisconnect

        try:
            raw = await request.body()
        except ClientDisconnect:
            raise RequestError(400, "invalid", "the request ended before its body") from None
        if not raw.strip():
            return {}
        try:
            body = json.loads(raw)
        except (ValueError, RecursionError):
            raise RequestError(400, "invalid", "the body must be a JSON object") from None
        if not isinstance(body, dict):
            raise RequestError(400, "invalid", "the body must be a JSON object")
        return body

    def kit_refuse(
        self,
        status: int,
        error: str,
        message: str | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        **extra: object,
    ) -> Response:
        """A refusal in the one shape every route answers with: ``{error, message}``.

        ``extra`` adds keys (a 409 ``stale`` carries ``current``) and ``headers``
        adds headers (a 429 carries ``Retry-After``).
        """
        from starlette.responses import JSONResponse

        body = {**_error_body(error, message), **extra}
        return JSONResponse(body, status_code=status, headers=dict(headers or {}))

    def kit_audit(self, device: Device, endpoint: str, summary: str) -> None:
        """One line in ``remote-audit.log`` for a write that went through."""
        self.runtime.audit(device.id, endpoint, summary)

    def kit_write_allowed(self) -> bool:
        """Whether writes are on right now (``remote.json``, re-read when it changes)."""
        return self.runtime.allow_write

    def kit_public_url(self) -> str | None:
        """``https://<host>/r/<token>/`` for a push link, or ``None`` when no origin is known.

        Never learned from a request: ``Host``, ``X-Forwarded-Host`` and
        ``X-Forwarded-Proto`` are whatever the sender wrote (SPEC §5.8). Only
        what :func:`check_public_origin` names as its sources noted it.
        """
        origin = self.runtime.remote_public_origin()
        return None if origin is None else f"{origin}/r/{self.runtime.token}/"

    def kit_pane_pool(self) -> ThreadPoolExecutor:
        """The pool every pane capture of the stream runs on, made on first use.

        Never the default thread pool, which serves every HTTP read and write:
        4 sockets x 8 subscriptions x N devices may queue captures here, but at
        most :data:`PANE_CAPTURE_WORKERS` run at once, and no request ever waits
        behind them.
        """
        with self._lock:
            if self.pane_pool is None:
                from concurrent.futures import ThreadPoolExecutor

                self.pane_pool = ThreadPoolExecutor(
                    max_workers=PANE_CAPTURE_WORKERS, thread_name_prefix="asq-remote-pane"
                )
            return self.pane_pool

    def kit_socket_opened(self, device_id: str, closer: Callable[[int], None]) -> None:
        """Count a device's new socket; past :data:`WS_SOCKETS_PER_DEVICE`, close its oldest."""
        with self._lock:
            live = self.sockets.setdefault(device_id, [])
            live.append(closer)
            evicted = live[: max(0, len(live) - WS_SOCKETS_PER_DEVICE)]
            del live[: len(evicted)]
        for close in evicted:
            try:
                close(WS_CLOSE_REPLACED)
            except Exception:  # a socket already gone must not cost the new one its handshake
                log.debug("remote: closing an evicted websocket failed", exc_info=True)

    def kit_socket_closed(self, device_id: str, closer: Callable[[int], None]) -> None:
        """Forget a socket that ended, evicted or not."""
        with self._lock:
            live = self.sockets.get(device_id, [])
            if closer in live:
                live.remove(closer)
            if not live:
                self.sockets.pop(device_id, None)

    def kit_route(
        self, path: str, endpoint: KitEndpoint, *, methods: list[str], write_gated: bool
    ) -> Route:
        """A lane's route: the endpoint gets the device and the parsed body (``{}`` for GET).

        With ``write_gated``, the route answers 403 ``read_only`` until writes are
        on, takes the optional ``request_id`` out of the body, answers a retried
        id from the ledger instead of running it again, refuses one still running
        (409 ``in_progress``), and stores how every request ended, refusals
        included, so a retry gets the answer the first try got.

        A route that changes something without the gate must be in
        :data:`NOT_WRITE_GATED`: anything else is refused here, when the app is
        built, rather than found in review.
        """
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        ungated = [
            m for m in methods if m not in ("GET", "HEAD") and (m, path) not in NOT_WRITE_GATED
        ]
        if ungated and not write_gated:
            raise ValueError(
                f"{ungated[0]} {path} changes something without the write gate "
                "and is not in NOT_WRITE_GATED"
            )
        name = path.removeprefix("/api/")

        async def kit_respond(request: Request, device: Device, body: dict[str, Any]) -> Response:
            try:
                return await endpoint(request, device, body)
            except RequestError as exc:
                return JSONResponse(exc.request_error_body(), status_code=exc.status)
            except LookupError as exc:
                return self.kit_refuse(404, "not_found", str(exc))

        async def kit_endpoint(request: Request) -> Response:
            reading = request.method in ("GET", "HEAD")
            gated = write_gated and not reading  # a read never waits on the write gate
            try:
                device = self.kit_device(request)
                if gated and not self.kit_write_allowed():
                    raise RequestError(403, "read_only", READ_ONLY_REASON)
                body = {} if reading else await self.kit_json_object(request)
                request_id = _ledger_request_id(body) if gated else None
            except RequestError as exc:
                return JSONResponse(exc.request_error_body(), status_code=exc.status)
            if request_id is None:
                return await kit_respond(request, device, body)
            replayed = self.ledger.ledger_replay(device.id, request_id)
            if replayed is not None:
                status, payload = replayed
                return JSONResponse(payload, status_code=status)
            if not self.ledger.ledger_begin(device.id, request_id, name):
                return self.kit_refuse(409, "in_progress", IN_PROGRESS)
            status, payload = 500, {"error": "internal_error"}
            try:
                response = await kit_respond(request, device, body)
                status, payload = response.status_code, _ledger_body(response)
                return response
            finally:  # even a crash is an ending: the id must never stay "running"
                self.ledger.ledger_finish(device.id, request_id, status, payload)

        return Route(path, kit_endpoint, methods=methods)


@contextlib.asynccontextmanager
async def remote_lifespan(kit: RemoteKit) -> AsyncIterator[None]:
    """The lanes' background work starts with the server and stops with it.

    The needs watcher first, then the push sender, which listens to it; at
    shutdown their stoppers run in reverse, and then the pane pool is shut
    down. A lane that fails to start costs its own feature and never the
    server: it is logged, and the rest carries on.
    """
    import asyncio

    from aisquare.services import remote_needs, remote_push

    stoppers: list[Callable[[], None]] = []
    for starter in (remote_needs.start_needs_watch, remote_push.start_push_sender):
        try:
            stopper = starter(kit)
        except Exception:
            name = getattr(starter, "__name__", repr(starter))
            log.warning("remote: %s failed; serving without it", name, exc_info=True)
            continue
        if stopper is not None:
            stoppers.append(stopper)
    try:
        yield
    finally:
        for stopper in reversed(stoppers):
            try:
                await asyncio.to_thread(stopper)
            except Exception:
                log.warning("remote: a lane did not stop cleanly", exc_info=True)
        pool, kit.pane_pool = kit.pane_pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)


def build_remote_app(
    runtime: Runtime,
    *,
    sources: Sources | None = None,
    writes: Writes | None = None,
    dist_dir: Path | None = None,
    tick: float = TICK_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    heartbeat: float = HEARTBEAT_SECONDS,
) -> _TokenGate:
    """The ASGI app. Everything real is behind ``sources``/``writes``; tests pass fakes."""
    # Here, not at module scope: `asq remote status`, `allow-write`, `revoke` and
    # `regenerate-password` import this module and serve nothing, and on Windows
    # importing asyncio opens a socket (``cli/remote.py`` says what that cost).
    import asyncio

    try:
        from starlette.applications import Starlette
        from starlette.responses import FileResponse, JSONResponse
        from starlette.routing import Mount, Route, WebSocketRoute
        from starlette.status import WS_1011_INTERNAL_ERROR
        from starlette.websockets import WebSocketDisconnect
    except ImportError as exc:  # pragma: no cover - exercised only in a base install
        raise RemoteUnavailable(f"the remote extra is not installed — {INSTALL_HINT}") from exc

    from aisquare.services import remote_actions, remote_needs, remote_push

    reads = sources or live_sources()
    handlers = (writes or live_writes()).handlers
    dist = (dist_dir or remote_dist_dir()).resolve()
    limiter = _RateLimiter(clock)
    budget = UnlockBudget(runtime)
    cache = _Cache(ttl=tick * 0.9)
    kit = RemoteKit(runtime, tick=tick)

    def cookie_path(request: Request) -> str:
        return f"/r/{request.path_params['token']}"

    async def snapshot(kind: str, compute: Snapshot) -> object:
        return await asyncio.to_thread(cache.cached_snapshot, kind, compute)

    def guarded(
        kind: str, compute: ProjectSource, *, scoped: bool = True
    ) -> Callable[[Request], Any]:
        """A cached read. ``?project=`` picks the project (``scoped``) and is part of the
        cache key, so a read of one project is never answered from another's snapshot
        (SPEC §7.5); an unknown project is a 404 ``not_found``."""

        async def guarded_read(request: Request) -> Response:
            project = (request.query_params.get("project") or None) if scoped else None
            try:
                payload = await snapshot(f"{kind}:{project or ''}", lambda: compute(project))
            except LookupError as exc:
                return _json_error(404, "not_found", str(exc))
            except Exception as exc:
                log.warning("remote: %s snapshot failed: %s", kind, exc)
                return _json_error(503, "unavailable", str(exc))
            return JSONResponse(payload)

        return guarded_read

    unlock_turn = threading.Lock()
    """Unlocks are decided one at a time: the budget's check and its record are then one
    step, so guesses that arrive together cannot all get past a budget with one left."""

    def unlock_decision(
        password: str, ua: str, cookie: str | None, direct: bool
    ) -> tuple[str, Device, bool] | datetime | None:
        """What an unlock comes to, decided in a worker thread: ``(secret, device,
        reactivated)`` for a right passphrase, ``None`` for a wrong one, and, while the
        budget is spent, when it opens again (the guess is not evaluated).

        Off the event loop because each step may write ``remote.json`` and so wait on
        another process's ``remote.json.lock``: run on the loop, one wrong guess while
        a CLI command held it stalled every socket and every read for seconds. A known
        device's last allowed wrong guess revokes it, and says so in the log and on
        the audit trail: that is most likely a stolen cookie, and its owner would
        otherwise find only a device gone.
        """
        with unlock_turn:
            known = runtime.known_device_for_cookie(cookie)
            if known is None and not budget.unlock_budget_allows(direct):
                return budget.budget_exhausted_until() or _remote_now()
            if known is None:
                unlocked = runtime.unlock_device(password, ua)
            elif runtime.password_matches(password):
                # Its own device again; one revoked or expired since the lookup is a new one.
                unlocked = runtime.reactivate_device(known.id, ua) or runtime.unlock_device(
                    password, ua
                )
            else:
                unlocked = None
            if unlocked is None:
                if known is None:
                    if not direct and budget.record_failed_unlock():
                        _unlock_lockout_alert(runtime)
                elif runtime.known_device_failed(known.id):
                    revoked = (
                        f"device {known.id} revoked after "
                        f"{KNOWN_DEVICE_FAILURES_MAX} wrong passwords sent with its cookie"
                    )
                    log.warning("remote: %s", revoked)
                    runtime.audit(known.id, "unlock", revoked)
                return None
            secret, device = unlocked
            reactivated = known is not None and device.id == known.id
            summary = f"device {device.id} " + ("reactivated" if reactivated else f"ua={ua[:60]}")
            runtime.audit(device.id, "unlock", summary)
            return secret, device, reactivated

    async def unlock_endpoint(request: Request) -> Response:
        """``POST api/unlock``: the passphrase for a cookie (SPEC §2.2).

        In order: the per-client limiter (every unlock); then, unless this is a
        phone that unlocked here before (its cookie names a known device) or the
        machine itself, the global failed-unlock budget, which refuses a spent
        budget WITHOUT evaluating the guess; then the passphrase. A wrong one counts
        against the known device's own cap, or against the budget (the machine's
        never does). A right one reactivates the known device under its old id, or
        makes a new one. Everything after the body is :func:`unlock_decision`'s.
        """
        retry = limiter.limiter_retry_after(_client_of(request.scope))
        if retry is not None:
            return kit.kit_refuse(
                429,
                "too_many_attempts",
                f"{UNLOCK_LIMIT} attempts a minute — wait",
                headers={"Retry-After": str(max(1, math.ceil(retry)))},
            )
        try:
            body = await kit.kit_json_object(request)
        except RequestError:
            body = {}
        password = body.get("password")
        if not isinstance(password, str):
            return _json_error(400, "invalid", 'send {"password": "..."}')
        decided = await asyncio.to_thread(
            unlock_decision,
            password,
            request.headers.get("user-agent", ""),
            request.cookies.get(COOKIE),
            is_direct_loopback(request.scope),
        )
        if isinstance(decided, datetime):
            wait = max(1, math.ceil((decided - _remote_now()).total_seconds()))
            return kit.kit_refuse(429, "locked_out", LOCKED_OUT, headers={"Retry-After": str(wait)})
        if decided is None:
            return _json_error(401, "wrong_password")
        secret, device, reactivated = decided
        # A reactivated device keeps its expiry, so its cookie gets what is left of it: a
        # cookie never outlives its device.
        expires = _remote_instant(device.expires_at) or _remote_now()
        left = (expires - _remote_now()).total_seconds() if reactivated else None
        response = JSONResponse(
            {"ok": True, "device": {"id": device.id, "expires_at": device.expires_at}}
        )
        response.set_cookie(
            COOKIE,
            secret,
            max_age=int(DEVICE_LIFETIME.total_seconds()) if left is None else max(0, int(left)),
            httponly=True,
            samesite="lax",
            path=cookie_path(request),
            # Secure only when the hop to the browser is TLS (ngrok says so in
            # X-Forwarded-Proto); on plain http://127.0.0.1 it would never be sent back.
            secure=_forwarded_https(request),
        )
        return response

    async def remote(request: Request) -> Response:
        return JSONResponse(runtime.remote_json())

    async def remote_extend_endpoint(
        request: Request, device: Device, body: dict[str, Any]
    ) -> Response:
        """``POST api/remote/extend``: another hour before auto-off (SPEC §2.5).

        Never more than :data:`AUTO_OFF_CEILING` ahead; a Remote with no deadline
        (Never) has nothing to extend, 409 ``no_auto_off``. The TUI adopts the later
        deadline (``RemoteController.enforce_auto_off``) and ``serve``'s timer re-arms.
        """
        extended = await asyncio.to_thread(runtime.extend_auto_off, _remote_now())
        if extended is None:
            return kit.kit_refuse(409, "no_auto_off", "Remote has no auto-off deadline to extend")
        stamp = _iso_seconds(extended)
        kit.kit_audit(device, "remote/extend", f"extend auto_off_at={stamp}")
        return JSONResponse({"auto_off_at": stamp})

    async def devices_list_endpoint(request: Request) -> Response:
        """Every device by id, with ``current`` for the caller's own: never a secret."""
        device = kit.kit_device(request)
        rows = [{**row, "current": row["id"] == device.id} for row in runtime.device_rows()]
        return JSONResponse(rows)

    async def devices_delete_endpoint(request: Request) -> Response:
        """Sign this device out, always; revoke ANOTHER device only while writes are on.

        Signing out removes the caller's device and clears its cookie, whatever
        the write switch says. Another id is a change to who can reach the fleet,
        so a read-only phone cannot sign every other phone out, the owner's
        included. An id that is not a device's shape, or no device's, is a 404.
        The revoke writes ``remote.json`` in a worker thread, as an unlock does.
        """
        device = kit.kit_device(request)
        device_id = request.path_params["device_id"]
        own = device_id == device.id
        if not own and not DEVICE_ID.fullmatch(device_id):
            return kit.kit_refuse(404, "not_found", "no such device")
        if not own and not kit.kit_write_allowed():
            return kit.kit_refuse(403, "read_only", READ_ONLY_REASON)
        if not await asyncio.to_thread(runtime.revoke_device, device_id):
            return kit.kit_refuse(404, "not_found", "no such device")
        kit.kit_audit(device, "devices/revoke", "self" if own else device_id)
        response = JSONResponse({"ok": True, "id": device_id, "signed_out": own})
        if own:
            response.delete_cookie(COOKIE, path=cookie_path(request))
        return response

    async def panes(request: Request) -> Response:
        agent = request.path_params["agent"]
        project = request.query_params.get("project") or None
        try:
            history = _history_param(request.query_params.get("history"))
        except ValueError as exc:
            return _json_error(400, "invalid", str(exc))
        try:
            payload = await asyncio.to_thread(reads.panes, agent, project, history)
        except NoSuchAgent as exc:  # gone: the page says so and goes back to the fleet
            return _json_error(404, "no_such_agent", str(exc))
        except LookupError as exc:
            return _json_error(404, "not_found", str(exc))
        except Exception as exc:
            log.warning("remote: pane capture for %s failed: %s", agent, exc)
            return _json_error(503, "unavailable", str(exc))
        return JSONResponse(payload)

    async def transcript(request: Request) -> Response:
        agent = request.path_params["agent"]
        project = request.query_params.get("project") or None
        before = request.query_params.get("before") or None
        try:
            limit = _limit_param(request.query_params.get("limit"))
            width = _width_param(request.query_params.get("width"))
        except ValueError as exc:
            return _json_error(400, "invalid", str(exc))
        try:
            payload = await asyncio.to_thread(
                reads.transcript, agent, project, limit, before, width
            )
        except NoSuchAgent as exc:
            return _json_error(404, "no_such_agent", str(exc))
        except LookupError as exc:
            return _json_error(404, "not_found", str(exc))
        except Exception as exc:
            log.warning("remote: transcript for %s failed: %s", agent, exc)
            return _json_error(503, "unavailable", str(exc))
        return JSONResponse(payload)

    async def explainability(request: Request) -> Response:
        agent = request.path_params["agent"]
        project = request.query_params.get("project") or None
        try:
            payload = await asyncio.to_thread(reads.explainability, agent, project)
        except NoSuchAgent as exc:
            return _json_error(404, "no_such_agent", str(exc))
        except LookupError as exc:
            return _json_error(404, "not_found", str(exc))
        except Exception as exc:  # §4-I: never raises, never blocks the other endpoints
            log.warning("remote: explainability for %s failed: %s", agent, exc)
            payload = {"available": False, "reason": f"explainability lookup failed: {exc}"}
        return JSONResponse(payload)

    async def write_endpoint(request: Request) -> Response:
        """``POST api/{name}``: the plan's writes and the agent actions (SPEC §1.5).

        In order: the name, the write gate, the body, the optional
        ``request_id`` and the ledger, the handler in a worker thread, the
        ledger again (refusals too, so a retry gets the same refusal), and the
        audit line for a write that went through.
        """
        device = kit.kit_device(request)
        name = request.path_params["name"]
        handler = handlers.get(name) if name in write_endpoint_names() else None
        if handler is None:
            return _json_error(404, "not_found")
        if not kit.kit_write_allowed():
            return kit.kit_refuse(403, "read_only", READ_ONLY_REASON)
        try:
            body = await kit.kit_json_object(request)
            request_id = _ledger_request_id(body)
        except RequestError as exc:
            return kit.kit_refuse(exc.status, exc.error, exc.message)
        if request_id is not None:
            replayed = kit.ledger.ledger_replay(device.id, request_id)
            if replayed is not None:
                status, payload = replayed
                return JSONResponse(payload, status_code=status)
            if not kit.ledger.ledger_begin(device.id, request_id, name):
                return kit.kit_refuse(409, "in_progress", IN_PROGRESS)
        summary: str | None = None
        try:
            result, summary = await asyncio.to_thread(handler, body)
            status, payload = 200, result
        except RequestError as exc:
            status, payload = exc.status, exc.request_error_body()
            summary = exc.audit  # a refusal that still did something is on the trail too
        except NoSuchAgent as exc:
            status, payload = 404, _error_body("no_such_agent", str(exc))
        except LookupError as exc:
            status, payload = 404, _error_body("not_found", str(exc))
        except Exception as exc:
            log.warning("remote: write %s failed: %s", name, exc)
            status, payload = 400, _error_body("write_failed", str(exc))
        if request_id is not None:
            kit.ledger.ledger_finish(device.id, request_id, status, payload)
        if summary is not None:
            kit.kit_audit(device, name, summary)
        return JSONResponse(payload, status_code=status)

    async def api_missing(request: Request) -> Response:
        return _json_error(404, "not_found")

    async def static(request: Request) -> Response:
        """The page: ``--dist``, else the installed build, else the one aisquare-cli bundles.

        Decided per request, so ``install-page`` takes over from the bundled page,
        and removing what it installed hands back, without a restart. Every answer
        carries the page headers (no referrer: the token is in the path); only the
        bundled page also gets its CSP, since an installed build may need another.
        """
        from aisquare.services import remote_page

        rel = request.path_params.get("path", "")
        index = dist / "index.html"
        if dist_dir is None and not index.is_file():
            bundled = remote_page.bundled_page_response(rel, request)
            if bundled is not None:
                return bundled
            response = _json_error(404, "no_dist", NO_PAGE_HINT)
        elif (
            rel
            and (candidate := (dist / rel).resolve()).is_relative_to(dist)
            and candidate.is_file()
        ):
            response = FileResponse(candidate, headers={"cache-control": _cache_control(rel)})
        elif rel and not _is_navigation(rel, request.headers.get("accept", "")):
            # A file was asked for and there is no such file. Saying so is the
            # whole point: the SPA document under a .js name is a boot failure
            # with no error, and the 200 hides which build is actually installed.
            response = _json_error(404, "not_found", f"no such file in the built page: {rel}")
        elif index.is_file():
            response = FileResponse(index, headers={"cache-control": INDEX_CACHE_CONTROL})
        else:
            response = _json_error(404, "no_dist", f"no index.html in {dist}")
        response.headers.update(remote_page.remote_page_headers())
        return response

    async def stream(websocket: WebSocket) -> None:
        """``/ws``: every tick, each frame that changed (SPEC §1.6).

        In order: ``board``, ``fleet``, ``remote``, then ``needs_you`` and
        ``action``, then the ``heartbeat`` (every ``heartbeat`` seconds, changed
        or not, never on the first tick), then one ``pane`` frame per
        subscription. Pane subscriptions are ``(project, label)``: the same
        label in two projects is two agents, and a frame names the project its
        subscription named. A lane seam that raises skips its own frame for the
        tick; anything else that fails ends the socket with 1011.
        """
        device = kit.kit_device(websocket)  # the gate refused a socket without one
        await websocket.accept()
        loop = asyncio.get_running_loop()
        panes_wanted: dict[tuple[str, str], str | None] = {}
        """``(project ref, label)`` per pane subscription, oldest first (``""`` is the CURRENT
        project), to the JSON of the last ``pane`` frame it was sent (``None`` before the
        first). A dict for its order: frames follow the order subscriptions came in. The
        last frame lives WITH its subscription, so unsubscribing forgets both: a socket
        that cycles through labels holds what its 8 subscriptions hold, and no more."""
        fleet_project: str | None = None
        """``None`` = the CURRENT project; a ``{subscribe_fleet: "<project>"}`` text frame
        picks another one's ``fleet`` frames (``""``/``null`` returns). The frame shape
        does not change, only WHICH project's ``fleet ls`` payload fills it."""
        board_project: str | None = None
        """The same, for ``board`` frames and ``{subscribe_board: "<project>"}``."""
        last: dict[str, str] = {}
        """The JSON of the last frame of every other kind, keyed by the kind alone and never by
        a string the client sent, so it cannot grow with what a client sends. Switching
        projects forgets that kind's frame, so the new project's goes out even if equal."""
        next_heartbeat = time.monotonic() + heartbeat
        first_tick = True
        lanes_failing: set[str] = set()
        """The lane seams that raised on this socket. The first failure of each is a warning
        with its traceback, any later one only a debug line: a lane broken for good would
        otherwise log a traceback on every tick of every socket."""

        async def close_with(code: int) -> None:
            with contextlib.suppress(Exception):
                await websocket.close(code=code)

        def closer(code: int) -> None:
            loop.call_soon_threadsafe(lambda: loop.create_task(close_with(code)))

        async def send_frame(
            kind: str, payload: object, *, agent: str | None = None, project: str | None = None
        ) -> None:
            frame: dict[str, object] = {"type": kind, "payload": payload, "ts": _stamp()}
            if agent is not None:
                frame["agent"] = agent
            if project is not None:
                frame["project"] = project
            await websocket.send_text(json.dumps(frame))

        async def push_if_changed(kind: str, payload: object) -> None:
            encoded = json.dumps(payload, sort_keys=True)
            if last.get(kind) != encoded:
                last[kind] = encoded
                await send_frame(kind, payload)

        def lane_frame_skipped(seam: str) -> None:
            """Called from an ``except``: a lane's bug costs its own frame, never the socket."""
            level = logging.DEBUG if seam in lanes_failing else logging.WARNING
            lanes_failing.add(seam)
            log.log(level, "remote: %s failed; the stream goes on without it", seam, exc_info=True)

        async def tick_once() -> None:
            nonlocal next_heartbeat, first_tick
            # A switch that lands while a snapshot is read must not let the old project's
            # frame out after it: the page would show it as the new one's until next tick.
            board_ref = board_project
            try:
                payload = await snapshot(f"board:{board_ref or ''}", lambda: reads.board(board_ref))
                if board_ref == board_project:
                    await push_if_changed("board", payload)
            except Exception as exc:
                log.debug("remote: board frame skipped: %s", exc)
            fleet_ref = fleet_project
            try:
                payload = await snapshot(f"fleet:{fleet_ref or ''}", lambda: reads.fleet(fleet_ref))
                if fleet_ref == fleet_project:
                    await push_if_changed("fleet", payload)
            except Exception as exc:
                log.debug("remote: fleet frame skipped: %s", exc)
            await push_if_changed("remote", runtime.remote_json())
            # The lanes' frames, each guarded as board and fleet are: one lane's bug,
            # raised here on every tick, would otherwise end every phone's live view.
            try:
                for kind, payload in remote_needs.needs_ws_frames(kit):
                    await push_if_changed(kind, payload)
            except Exception:
                lane_frame_skipped("needs_ws_frames")
            try:
                actions = kit.ledger.ledger_recent(device.id)
                if actions:
                    await push_if_changed("action", {"actions": actions})
            except Exception:
                lane_frame_skipped("ledger_recent")
            now = time.monotonic()
            if first_tick:
                first_tick = False
            elif now >= next_heartbeat:
                next_heartbeat = now + heartbeat
                try:
                    scanned = remote_needs.needs_scanned_iso(kit)
                except Exception:
                    lane_frame_skipped("needs_scanned_iso")
                    scanned = None  # the beat says the LINK is alive: it goes out regardless
                await send_frame("heartbeat", {"needs_scanned_at": scanned})
            for wanted in list(panes_wanted):
                project, label = wanted
                try:
                    # §4-L: history is a FETCH, live stays a stream — 0 keeps
                    # this frame the live shape, with no history keys.
                    payload = await loop.run_in_executor(
                        kit.kit_pane_pool(), reads.panes, label, project or None, 0
                    )
                except Exception as exc:
                    payload = {"rows": [], "width": 0, "height": 0, "error": str(exc)}
                encoded = json.dumps(payload, sort_keys=True)
                # Not wanted any more: unsubscribed while the capture ran, so no frame,
                # and nothing kept for it either.
                if wanted in panes_wanted and panes_wanted[wanted] != encoded:
                    panes_wanted[wanted] = encoded
                    await send_frame("pane", payload, agent=label, project=project or None)

        async def reader() -> None:
            nonlocal fleet_project, board_project
            while True:
                received = await websocket.receive()
                if received["type"] == "websocket.disconnect":
                    raise WebSocketDisconnect(received.get("code", 1000))
                text = received.get("text")
                if not isinstance(text, str) or len(text) > WS_CLIENT_MESSAGE_MAX:
                    continue  # bytes, or longer than any message a client sends: ignored
                try:
                    message = json.loads(text)
                except (ValueError, RecursionError):
                    continue  # RecursionError: nested past the parser's depth, 1 000 on 3.11
                if not isinstance(message, dict):
                    continue
                ref = message.get("project")
                project = ref if isinstance(ref, str) else ""
                label = message.get("subscribe")
                if isinstance(label, str) and label:
                    if (project, label) in panes_wanted or len(
                        panes_wanted
                    ) < WS_PANE_SUBSCRIPTIONS_MAX:
                        panes_wanted[(project, label)] = None  # its frame goes out next tick
                    else:
                        refusal = _error_body(
                            "too_many_subscriptions",
                            f"one socket watches at most {WS_PANE_SUBSCRIPTIONS_MAX} panes "
                            "— unsubscribe one first",
                        )
                        await send_frame("error", refusal)
                label = message.get("unsubscribe")
                if isinstance(label, str):
                    panes_wanted.pop((project, label), None)
                target = message.get("subscribe_fleet", False)
                if target is None or isinstance(target, str):
                    fleet_project = target or None
                    last.pop("fleet", None)
                target = message.get("subscribe_board", False)
                if target is None or isinstance(target, str):
                    board_project = target or None
                    last.pop("board", None)

        runtime.register_socket(device.id, closer)
        kit.kit_socket_opened(device.id, closer)
        reading = asyncio.ensure_future(reader())
        try:
            while not reading.done():
                # By id, every tick: Remote off (auto-off included) is 4410, a device that is
                # gone, expired or idle past the limit is 4401, whatever the cookie said.
                if runtime.auto_off_passed(_remote_now()):
                    await close_with(WS_CLOSE_REMOTE_OFF)
                    break
                if not runtime.device_is_live(device.id):
                    await close_with(WS_CLOSE_UNAUTHORIZED)
                    break
                await tick_once()
                await asyncio.wait([reading], timeout=tick)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            log.debug("remote: stream for %s ended: %s", device.id, exc)
            # A failure, said as one (the page reconnects with backoff): returning
            # without a close leaves the phone an abnormal 1006 and no reason.
            await close_with(WS_1011_INTERNAL_ERROR)
        finally:
            kit.kit_socket_closed(device.id, closer)
            runtime.unregister_socket(device.id, closer)
            reading.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reading

    api_routes = [
        Route("/api/unlock", unlock_endpoint, methods=["POST"]),
        Route("/api/remote", remote, methods=["GET"]),
        Route(
            "/api/projects",
            guarded("projects", lambda _project: reads.projects(), scoped=False),
            methods=["GET"],
        ),
        Route("/api/fleet", guarded("fleet", reads.fleet), methods=["GET"]),
        Route("/api/board", guarded("board", reads.board), methods=["GET"]),
        Route("/api/tasks", guarded("tasks", reads.tasks), methods=["GET"]),
        Route("/api/memory", guarded("memory", reads.memory), methods=["GET"]),
        Route("/api/devices", devices_list_endpoint, methods=["GET"]),
        Route("/api/devices/{device_id}", devices_delete_endpoint, methods=["DELETE"]),
        Route("/api/panes/{agent}", panes, methods=["GET"]),
        Route("/api/transcript/{agent}", transcript, methods=["GET"]),
        Route("/api/explainability/{agent}", explainability, methods=["GET"]),
        # The lanes' routes: after the built-in reads, before the write catch-all.
        kit.kit_route(
            "/api/remote/extend", remote_extend_endpoint, methods=["POST"], write_gated=True
        ),
        *remote_needs.needs_routes(kit),
        *remote_push.push_routes(kit),
        *remote_actions.action_routes(kit),
        Route("/api/{name:path}", write_endpoint, methods=["POST"]),
        Route("/api/{rest:path}", api_missing),
        WebSocketRoute("/ws", stream),
        Route("/", static, methods=["GET"]),
        Route("/{path:path}", static, methods=["GET"]),
    ]
    inner = Starlette(
        routes=[Mount("/r/{token}", routes=api_routes)], lifespan=lambda app: remote_lifespan(kit)
    )
    return _TokenGate(inner, runtime, kit)


build_app = build_remote_app
"""``build_app`` is the name every caller uses (SPEC §1). The def carries its area prefix
because #240 defines a ``build_app`` of its own that the hook path reaches by bare name
(``serve`` → the captain's voice ``serve`` → ``build_app``), and a remote def of that
name would pull this whole app into the graph the config-write guard walks. An
assignment is not a def, so the alias bridges nothing."""


# --- process lifecycle: the module API the TUI modal calls (PLAN §4-F) ----------------


def _remote_dependency_error() -> str | None:
    import importlib.util

    missing = [
        name
        for name in ("starlette", "uvicorn", "websockets")
        if importlib.util.find_spec(name) is None
    ]
    if not missing:
        return None
    return f"the remote extra is not installed ({', '.join(missing)} missing) — {INSTALL_HINT}"


def runtime() -> Runtime:
    """The process-wide state, loaded from ``remote.json`` on first use."""
    global _runtime
    with _lock:
        if _runtime is None:
            ensure_home()
            _runtime = Runtime(remote_state_path(), remote_audit_path())
        return _runtime


def _remote_uvicorn_config(app: Any, port: int) -> uvicorn.Config:
    """uvicorn's settings for this server, in the TUI's thread and under ``serve`` alike.

    ``proxy_headers`` with ``forwarded_allow_ips`` spelled out: only the hop from
    127.0.0.1 (ngrok's agent) may say who the client is and that it came over
    https, and the ``FORWARDED_ALLOW_IPS`` environment variable cannot widen that.
    uvicorn then takes the rightmost ``X-Forwarded-For`` entry that is not trusted,
    the one ngrok appended (:func:`_client_of`). A WebSocket message is capped at
    :data:`WS_MAX_MESSAGE_BYTES`, where uvicorn's own default is 16 MiB.
    """
    import uvicorn

    return uvicorn.Config(
        app,
        host=BIND,
        port=port,
        log_level="warning",
        ws="auto",
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        ws_max_size=WS_MAX_MESSAGE_BYTES,
    )


class _Server:
    """uvicorn in a daemon thread, stopped by flipping ``should_exit``."""

    def __init__(self, app: Any, port: int) -> None:
        import uvicorn

        self.port = port
        self._server = uvicorn.Server(_remote_uvicorn_config(app, port))
        self._thread = threading.Thread(
            target=self._serve_in_thread, name="asq-remote", daemon=True
        )

    def _serve_in_thread(self) -> None:
        # uvicorn answers a failed bind with sys.exit(3); in a thread that is
        # noise, and start_serving() already turns "never started" into a RemoteError.
        with contextlib.suppress(SystemExit):
            self._server.run()

    def start_serving(self, timeout: float = 5.0) -> None:
        self._thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._server.started:
                return
            if not self._thread.is_alive():
                break
            time.sleep(0.02)
        self._server.should_exit = True
        raise RemoteError(
            f"the remote server did not come up on {BIND}:{self.port} — is the port in use?"
        )

    def stop_serving(self, timeout: float = 5.0) -> None:
        self._server.should_exit = True
        self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return self._thread.is_alive() and self._server.started


_lock = threading.Lock()
_runtime: Runtime | None = None
_server: _Server | None = None
_foreground: uvicorn.Server | None = None
"""The server :func:`run_foreground` runs (``asq remote serve``), while it runs."""
_flusher: threading.Timer | None = None


def _page_missing(dist_dir: Path | None) -> str | None:
    """``None`` when there is a page to serve; otherwise the sentence that says why not.

    Checked up front by :func:`start_remote_server`, :func:`run_foreground` and
    ``asq remote serve``, not by :func:`build_app` itself: an explicit
    ``--dist``/``dist_dir`` that turns out to be wrong is still a per-request 404
    there (``test_missing_dist_is_a_404_...``), because the caller named that path
    on purpose and may still be building it. Without one, the installed build
    (:func:`install_page`) is served, else the page aisquare-cli bundles, so a
    fresh machine's first ``R`` press just works. Only an install that lost its
    bundled page gets :data:`NO_PAGE_HINT` instead of a server that answers every
    request with nothing.
    """
    if dist_dir is not None:
        dist = dist_dir.resolve()
        return None if (dist / "index.html").is_file() else f"no index.html in {dist}"
    if (remote_dist_dir().resolve() / "index.html").is_file():
        return None
    from aisquare.services import remote_page

    return None if remote_page.bundled_page_present() else NO_PAGE_HINT


def install_page(source: Path) -> Path:
    """Copy a built ``aisquare-remote`` dist into :func:`remote_dist_dir`, atomically.

    ``source`` must contain ``index.html`` (re-checked here even though the CLI
    command already does, so a direct caller gets the same guard). The copy
    lands in a staging directory beside the destination and is swapped in with
    two renames — same filesystem, so each rename is atomic — rather than
    removing the destination first, so a server reading the old page mid-swap
    never sees a half-written one.
    """
    source = source.resolve()
    if not (source / "index.html").is_file():
        raise NoRemotePage(f"no index.html in {source} — build aisquare-remote first")
    ensure_home()
    destination = remote_dist_dir()
    staging = destination.with_name(f".{destination.name}.staging-{os.getpid()}")
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(source, staging)
    previous = destination.with_name(f".{destination.name}.previous-{os.getpid()}")
    shutil.rmtree(previous, ignore_errors=True)
    if destination.exists():
        destination.rename(previous)
    try:
        staging.rename(destination)
    except OSError:
        if previous.exists() and not destination.exists():
            previous.rename(destination)
        raise
    shutil.rmtree(previous, ignore_errors=True)
    return destination


def start_remote_server(dist_dir: Path | None = None, port: int = DEFAULT_PORT) -> RemoteInfo:
    """Serve in the background; idempotent while running. ``allow_write`` is left as persisted."""
    global _server
    problem = _remote_dependency_error()
    if problem is not None:
        raise RemoteUnavailable(problem)
    page_problem = _page_missing(dist_dir)
    if page_problem is not None:
        raise NoRemotePage(page_problem)
    state = runtime()
    with _lock:
        if _server is not None and _server.running:
            return state.connection_info(_server.port)
        app = build_remote_app(state, dist_dir=dist_dir)
        server = _Server(app, port)
        server.start_serving()
        _server = server
    _schedule_flush()
    return state.connection_info(port)


def stop_remote_server() -> None:
    """Stop the background server (no-op when it is not running).

    The server stops first and ``remote.json`` is flushed last, best effort, as the
    flusher's every-30-s write is: a file that will not write is logged, never
    raised. The TUI turns Remote off from a Textual timer (auto-off), where an
    exception ends the whole fleet UI, and stops ngrok only once this returns.
    """
    global _server, _flusher
    with _lock:
        server, _server = _server, None
        flusher, _flusher = _flusher, None
    if flusher is not None:
        flusher.cancel()
    if server is not None:
        server.stop_serving()
    if _runtime is not None:
        try:
            _runtime.flush_last_seen()
        except Exception:  # the server is already down; only last_seen is lost
            log.warning("remote: flushing remote.json as the server stopped failed", exc_info=True)


def remote_server_status() -> dict[str, object]:
    """``{running, devices, failed_unlocks, locked_out_until}`` (PLAN §4-F, SPEC §2.2, §2.3).

    ``devices`` are :meth:`Runtime.device_rows`, by id, with no secret. The
    failed-unlock budget is read from ``remote.json``, where the server keeps it,
    so a ``status`` in another shell sees what the server enforces.
    """
    with _lock:
        running = _server is not None and _server.running
    state = runtime()
    budget = UnlockBudget(state)
    until = budget.budget_exhausted_until()
    return {
        "running": running,
        "devices": state.device_rows(),
        "failed_unlocks": budget.budget_failures(),
        "locked_out_until": None if until is None else _iso_seconds(until),
    }


def revoke_remote_device(device_id: str) -> bool:
    """Remove one device by id and close its sockets with 4401; ``True`` if it existed."""
    return runtime().revoke_device(device_id)


def revoke_every_remote_device(reason: str) -> None:
    """Remote is going off: tell the phones, then revoke every device with 4410 (SPEC §2.4).

    The farewell push goes first, to the devices about to be revoked, since a
    revoked device's subscription is dropped; it is sent from a daemon thread and
    never waits on the network here. The TUI's switch and both auto-offs come
    here; ``asq remote revoke --all`` does not (Remote stays on, phones unlock again).
    """
    from aisquare.services import remote_push

    state = runtime()
    try:
        remote_push.push_farewell(state.device_ids(), reason)
    except Exception:  # a push that cannot be queued must not keep Remote on
        log.warning("remote: the farewell push could not be queued", exc_info=True)
    state.revoke_every_device(reason, close_code=WS_CLOSE_REMOTE_OFF)


def set_allow_write(enabled: bool) -> None:
    """Flip the write gate — the ONLY way it turns on; the default is off."""
    runtime().set_allow_write(enabled)


def regenerate_password(new_link: bool = False) -> str:
    """A fresh password; every device has to unlock again. ``new_link``: a new link too."""
    return runtime().regenerate_password(new_link=new_link)


def note_public_url(url: str | None) -> None:
    """Tell this process's server where phones reach it; ``None``: nowhere any more.

    The TUI calls it once ngrok announces its URL, and with ``None`` when Remote
    goes off; ``asq remote serve --public-url`` calls it at start. A URL that is
    not https on a DNS name raises ``ValueError`` (:func:`check_public_origin`).
    """
    runtime().note_public_origin(None if url is None else check_public_origin(url))


def set_auto_off(at: datetime | None) -> None:
    """Record when the modal will switch Remote off (shown as ``auto_off_at``)."""
    runtime().set_auto_off(at)


def remote_auto_off_at() -> datetime | None:
    """When Remote turns itself off, as ``remote.json`` says now: a phone may have extended it."""
    return runtime().auto_off_deadline()


def remote_allow_write() -> bool:
    """Whether writes are on: ``remote.json``'s switch, the ONLY one (the modal reads it here)."""
    return runtime().allow_write


def remote_password() -> str:
    """The passphrase as ``remote.json`` says now, ``regenerate-password`` from a shell included."""
    return runtime().password


def _schedule_flush() -> None:
    """Persist ``last_seen`` and prune expired devices every 30 s while serving.

    For the TUI's server and for ``asq remote serve`` alike: the flusher used to
    re-arm only while the TUI's ran, so ``serve`` never wrote ``last_seen`` at all.
    """
    global _flusher

    def flush_and_rearm() -> None:
        with _lock:
            serving = (_server is not None and _server.running) or _foreground is not None
        if _runtime is not None:
            try:
                _runtime.flush_last_seen()
            except Exception:  # one failed write must not end the flushing for good
                log.warning("remote: flushing remote.json failed", exc_info=True)
        if serving:
            _schedule_flush()

    with _lock:
        if _flusher is not None:
            _flusher.cancel()
        _flusher = threading.Timer(30.0, flush_and_rearm)
        _flusher.daemon = True
        _flusher.start()


class _AutoOffTimer:
    """``serve``'s auto-off: at the deadline, Remote turns off, unless a phone moved it later.

    Armed for the deadline ``remote.json`` holds, but never for more than
    :data:`AUTO_OFF_CHECK_SECONDS` at a time; when it fires it reads the
    deadline and the wall clock again, and waits on for a deadline a phone
    extended (``POST api/remote/extend``) or one not reached yet. Only a
    deadline really past calls ``turn_off``.

    In slices because a timer counts the monotonic clock, which stands still
    while the machine sleeps, and the deadline is wall-clock time, which the
    gate and the stream read. Armed once for the whole delay, a laptop that
    slept past the deadline left ``serve`` half off for up to the time still
    owed when it woke: every request a 404 and every socket closed, but no
    device revoked, no farewell, the process still up and pushing.
    """

    def __init__(
        self,
        state: Runtime,
        turn_off: Callable[[], None],
        *,
        timer: Callable[[float, Callable[[], None]], Any] = threading.Timer,
    ) -> None:
        self._state = state
        self._turn_off = turn_off
        self._timer_factory = timer
        self._timer: Any = None
        self._lock = threading.Lock()
        self.fired = False
        """Whether the deadline passed and Remote was turned off."""

    def auto_off_arm(self) -> None:
        """Wait toward the deadline ``remote.json`` holds now; none at all is never."""
        deadline = self._state.auto_off_deadline()
        if deadline is None:
            return
        delay = min(max(0.0, (deadline - _remote_now()).total_seconds()), AUTO_OFF_CHECK_SECONDS)
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = self._timer_factory(delay, self.auto_off_fire)
            self._timer.daemon = True
            self._timer.start()

    def auto_off_fire(self) -> None:
        deadline = self._state.auto_off_deadline()
        if deadline is None:
            return
        if deadline > _remote_now():
            self.auto_off_arm()  # not yet, or extended from a phone meanwhile
            return
        self.fired = True
        self._turn_off()

    def auto_off_cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None


def _remote_serve_off(state: Runtime, server: Any) -> None:
    """``serve``'s auto-off firing: the farewell, every device revoked (4410), the deadline
    cleared, and the server told to stop, even when ``remote.json`` cannot be written."""
    try:
        revoke_every_remote_device("auto-off")
        state.set_auto_off(None)
    finally:
        server.should_exit = True


class RemoteBindError(RemoteError):
    """``serve``'s port could not be bound: another process holds it, or it is not ours.

    Its own class, so the CLI calls only THIS a bind failure: it caught every
    ``OSError`` out of :func:`run_foreground`, and a ``remote.json`` that would not
    write was reported as "cannot bind 127.0.0.1:8750"."""


def _bind_remote_socket(port: int) -> socket.socket:
    """A socket bound to ``127.0.0.1:port`` for uvicorn to serve on; :class:`RemoteBindError`
    when that port cannot be had.

    Bound here, before anything is printed: uvicorn binding for itself turned a
    taken port into ``sys.exit(3)`` AFTER ``serve`` had printed its banner and the
    ``--json`` success payload. Address reuse only on POSIX, where it means "past
    TIME_WAIT"; on Windows it would mean sharing a port another process holds.

    It listens at once, too: the banner goes out before uvicorn has started, and a
    script reading the ``--json`` link (or ngrok, or a phone) that connected in
    between was refused. A connection now waits in the backlog for uvicorn, whose
    own ``listen`` on the same socket only sets the backlog again.
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "posix":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((BIND, port))
        sock.listen()
    except OSError as exc:
        sock.close()
        raise RemoteBindError(f"cannot bind {BIND}:{port} — {exc}") from exc
    return sock


def run_foreground(
    dist_dir: Path | None = None,
    port: int = DEFAULT_PORT,
    auto_off_minutes: int = 0,
    public_url: str | None = None,
    *,
    ready: Callable[[], None] | None = None,
) -> bool:
    """``asq remote serve``: serve in this thread until Ctrl-C or auto-off.

    ``True`` when auto-off ended it. In order: the port is bound (:class:`RemoteBindError`
    when another process holds it, before ``ready`` prints anything); the deadline is set
    ``auto_off_minutes`` from now (0 is never) and ``public_url`` noted as the origin
    of push links; ``ready`` runs (the CLI's banner); uvicorn serves on the bound
    socket. A timer that reads the wall clock every 30 s turns Remote off at the
    deadline, or at the first check after the machine slept past it, and waits
    on while a phone keeps extending it, with the farewell push and every device
    revoked (4410); the flusher writes ``last_seen`` and prunes devices every
    30 s. Ctrl-C revokes nothing (SPEC §2.4): the devices' own expiry bounds them.
    """
    global _foreground, _flusher
    problem = _remote_dependency_error()
    if problem is not None:
        raise RemoteUnavailable(problem)
    page_problem = _page_missing(dist_dir)
    if page_problem is not None:
        raise NoRemotePage(page_problem)
    import uvicorn

    origin = None if public_url is None else check_public_origin(public_url)
    state = runtime()
    sock = _bind_remote_socket(port)
    try:
        app = build_remote_app(state, dist_dir=dist_dir)
        server = uvicorn.Server(_remote_uvicorn_config(app, port))

        timer = _AutoOffTimer(state, lambda: _remote_serve_off(state, server))
        minutes = max(0, auto_off_minutes)
        state.set_auto_off(_remote_now() + timedelta(minutes=minutes) if minutes else None)
        state.note_public_origin(origin)
        with _lock:
            _foreground = server
        timer.auto_off_arm()
        _schedule_flush()
        try:
            if ready is not None:
                ready()
            server.run(sockets=[sock])
        except KeyboardInterrupt:  # uvicorn re-raises the Ctrl-C it caught, once it stopped
            pass
        except SystemExit as exc:  # uvicorn's way to say it could not start
            raise RemoteError(f"the remote server stopped (exit {exc.code})") from None
        finally:
            timer.auto_off_cancel()
            with _lock:
                _foreground = None
                flusher, _flusher = _flusher, None
            if flusher is not None:
                flusher.cancel()
            try:
                if minutes and not timer.fired:
                    state.set_auto_off(None)  # no server, no deadline: nothing stays on to end
                state.flush_last_seen()
            except Exception:  # the way out reports what ended the server, not this
                log.warning("remote: writing remote.json on the way out failed", exc_info=True)
        return timer.fired
    finally:
        sock.close()


__all__ = [
    "ASSET_CACHE_CONTROL",
    "BIND",
    "COOKIE",
    "DEFAULT_PORT",
    "HISTORY_CAP",
    "INDEX_CACHE_CONTROL",
    "NO_PAGE_HINT",
    "READ_ONLY_REASON",
    "WRITE_ENDPOINTS",
    "WS_CLOSE_UNAUTHORIZED",
    "Device",
    "DoctorVerdict",
    "NoRemotePage",
    "NoSuchAgent",
    "NoSuchProject",
    "RemoteBindError",
    "RemoteError",
    "RemoteInfo",
    "RemoteUnavailable",
    "RequestError",
    "Runtime",
    "Sources",
    "Writes",
    "build_app",
    "build_local_url",
    "build_remote_app",
    "check_public_origin",
    "explainability_payload",
    "install_page",
    "live_sources",
    "live_writes",
    "note_public_url",
    "regenerate_password",
    "remote_board_payload",
    "remote_gate_token",
    "remote_server_status",
    "revoke_remote_device",
    "run_foreground",
    "runtime",
    "set_allow_write",
    "set_auto_off",
    "start_remote_server",
    "stop_remote_server",
]
