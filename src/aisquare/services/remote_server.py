"""The Remote Control server: one local port that shows the fleet to a phone.

``asq remote serve`` runs it in the foreground; the fleet UI's Remote modal runs
it in a background thread through :func:`start_remote_server` /
:func:`stop_remote_server`. Either way it
binds ``127.0.0.1`` only — ngrok (or the same machine's browser) is the only way
in — and every path lives under ``/r/<token>/``:

* ``/r/<token>/``                 the built ``aisquare-remote`` page (SPA fallback)
* ``POST /r/<token>/api/unlock``  ``{password}`` → ``Set-Cookie: asq_remote=<sid>``
* ``GET  /r/<token>/api/...``     ``projects fleet board tasks memory panes/<agent>
                                  explainability/<agent> devices remote`` — the read-only JSON.
                                  ``fleet`` and ``panes/<agent>`` take an optional
                                  ``?project=<id|name|codename>`` (default: the
                                  CURRENT project, unchanged); an unknown project
                                  is a 404 shaped exactly like an unknown agent.
                                  ``transcript/<agent>`` pages the agent's own
                                  conversation (``?limit=``, ``?before=``, §4-M) —
                                  the pane cannot, being alternate-screen.
                                  ``panes/<agent>`` also takes ``?history=<n>``
                                  for n lines of scrollback above the live screen,
                                  oldest first in one block (§4-L); omitted or 0
                                  is today's live-only frame, byte for byte.
* ``WS   /r/<token>/ws``          frames ``{type, agent?, payload, ts}`` every second;
                                  a ``{subscribe_fleet:"<project>"}`` text frame
                                  switches which project's ``fleet`` frames arrive
                                  (empty string/``null`` returns to the current one)
* ``POST /r/<token>/api/...``     the write endpoints: 403 unless ``allow_write``

Three gates, in this order. A wrong or missing token is a **404** on everything,
so the URL alone leaks nothing — not even that a server is here. A missing or
revoked cookie is a **401** on ``/api`` and ``/ws`` (the page itself needs none,
or the unlock screen could not load). Unlock takes five attempts a minute per
client, then **429**. Write endpoints exist from day one and answer **403** until
``allow_write`` is switched on (default OFF, never on by itself); each write that
does go through appends one line to ``remote-audit.log``.

The read-only payloads are exactly what ``asq --json`` prints: the handlers call
the same builders the typer commands use (``projects_json``, ``agents_json``,
``board_json``, the task and entry model dumps), so nobody invents a field here.
Remote-specific state has its own endpoint, ``GET /api/remote``.

State (token, password, ``allow_write``, ``auto_off_at``, devices) lives in
``~/.aisquare/remote.json``, owner-only (0600; on Windows, a DACL for this
account alone), and a serving process re-reads it when its bytes change —
``aisquare remote allow-write on`` from another shell reaches the
running server within a second (:meth:`Runtime.reload_if_changed`). Everything
that touches real systems goes through :class:`Sources` and :class:`Writes`, two
bags of callables the tests replace — the server itself never opens the store or
spawns tmux.

Dependencies: starlette and uvicorn (already here through the ``serve`` extra) and
``websockets`` (uvicorn's WebSocket backend) — the ``remote`` extra in pyproject.
All three are imported lazily so this module, and the modal that imports it,
load in a base install; :func:`start_remote_server` and the CLI say what to install.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from aisquare.core.atomic import write_replacing
from aisquare.core.paths import (
    ensure_home,
    remote_audit_path,
    remote_dist_dir,
    remote_state_path,
    restrict_to_owner,
)
from aisquare.core.version import __version__
from aisquare.models import FleetAgent, FleetAgentStatus, ProjectInfo, TeamSession, TurnMetric

if TYPE_CHECKING:
    from concurrent.futures import ThreadPoolExecutor

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
WS_CLOSE_UNAUTHORIZED = 4401
"""Close code the page keys on: after it the adapter routes to /unlock."""
WS_CLOSE_BAD_ORIGIN = 4403
"""A handshake from another origin, where the server offers no denial response."""
WS_CLOSE_NOT_FOUND = 4404
"""A wrong token (or Remote off), where the server offers no denial response."""
WS_CLOSE_REPLACED = 4409
"""The same device opened one socket too many and this, its oldest, made way: the page
does not reconnect it until its tab is visible again (SPEC §2.10)."""

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

MAX_BODY_BYTES = 65_536
"""The largest body any request may carry, refused with 413 before a route sees it.

``unlock`` is covered too: it is the one body anyone holding only the URL can send.
Every legitimate body is a few hundred bytes; the longest text a phone may type
(``agent/tell``, 8 000 characters of 4-byte UTF-8, JSON-escaped) still fits."""

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

READ_ONLY_REASON = "read-only build (allow write actions is off in the TUI)"
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

NO_PAGE_HINT = "no remote page installed — run: aisquare remote install-page <dist>"
"""Shown by the modal's status line, ``asq remote serve``'s exit, and the raise of
``start_remote_server()`` — one sentence, so a fresh machine never sees a server that
quietly answers with nothing."""

_PASSPHRASE_WORDS = (
    "amber", "birch", "cedar", "delta", "ember", "fjord", "glade", "harbor",
    "indigo", "juniper", "kestrel", "lagoon", "meadow", "nectar", "orchid", "pebble",
    "quartz", "river", "saffron", "tundra", "umber", "velvet", "willow", "yarrow",
    "zenith", "anchor", "beacon", "canyon", "dune", "falcon", "garnet", "heron",
)  # fmt: skip
"""Phone-typeable, lower-case, no look-alike letters: 4 distinct words of 32 ≈ 19 bits
on top of the 32-character URL token (the modal task and RABIA-HANDOFF call for a passphrase)."""
PASSPHRASE_WORDS = 4
"""Typed on a phone, read off a terminal: no 0/O, 1/l/I."""


class RemoteError(RuntimeError):
    """The server could not do what was asked (start, stop, revoke)."""


class RemoteUnavailable(RemoteError):
    """The ``remote`` extra is not installed."""


class NoRemotePage(RemoteError):
    """No built ``aisquare-remote`` page is installed at the directory the server would serve."""


class RequestError(Exception):
    """A handler's refusal, carried to the client as ``{error, message}``."""

    def __init__(self, status: int, error: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message


class NoSuchAgent(LookupError):
    """``panes/<agent>`` or ``send-keys`` named an agent the project does not have."""


class NoSuchProject(LookupError):
    """A ``?project=`` query or a write body's ``project`` field matches no project.

    ``fleet_service.resolve_project`` raises its own ``NoSuchProject`` (not a
    ``LookupError``) for both "no match" and "ambiguous" — folded into this one
    ``LookupError`` shape by :func:`_resolve_project` so every existing
    ``except LookupError`` (the GET routes, the write dispatcher, the WS tick)
    turns it into the SAME 404 shape an unknown agent gets — no new branch.
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
    """A 4-word hyphenated passphrase of DISTINCT words from :data:`_PASSPHRASE_WORDS`."""
    import secrets

    return "-".join(secrets.SystemRandom().sample(_PASSPHRASE_WORDS, PASSPHRASE_WORDS))


def _same(supplied: str, expected: str) -> bool:
    """Constant-time equality on the UTF-8 bytes (str-mode rejects non-ASCII)."""
    import secrets

    return secrets.compare_digest(supplied.encode("utf-8", "replace"), expected.encode())


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
    TUI's ngrok announcement, ``serve --public-url`` or ngrok's own agent API,
    and NEVER from a request header, which anyone who reaches the server
    writes (SPEC §5.8). The path is dropped: ``/r/<token>/`` is the server's.
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


@dataclass
class Device:
    """One unlocked browser: the cookie session and what it told us about itself."""

    sid: str
    ua: str
    first_seen: str
    last_seen: str
    id: str = field(init=False, compare=False, repr=False)
    """The name every caller uses for this device: routes, the audit log, the socket
    registry. Until devices get ids of their own that are not their cookies (SPEC §2.3),
    it is the session id. A field and not a property: on the merge with #240 the hook
    path calls ``id(...)``, so a remote ``def id`` would be a bare name it reaches, which
    the naming test (``test_remote_names_stay_off_the_hook_graph.py``) forbids."""

    def __post_init__(self) -> None:
        self.id = self.sid

    def device_json(self) -> dict[str, str]:
        return {
            "sid": self.sid,
            "ua": self.ua,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }


@dataclass
class RemoteInfo:
    """What the modal shows: the link and the password (PLAN §4-F)."""

    token: str
    password: str
    url_local: str


@dataclass
class _State:
    token: str
    password: str
    allow_write: bool = False
    auto_off_at: str | None = None
    sessions: list[Device] = field(default_factory=list)

    def state_json(self) -> dict[str, object]:
        return {
            "token": self.token,
            "password": self.password,
            "allow_write": self.allow_write,
            "auto_off_at": self.auto_off_at,
            "sessions": [device.device_json() for device in self.sessions],
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> _State:
        token = raw.get("token")
        password = raw.get("password")
        devices: list[Device] = []
        for row in raw.get("sessions") or []:
            if isinstance(row, dict) and isinstance(row.get("sid"), str):
                devices.append(
                    Device(
                        sid=row["sid"],
                        ua=str(row.get("ua") or ""),
                        first_seen=str(row.get("first_seen") or ""),
                        last_seen=str(row.get("last_seen") or ""),
                    )
                )
        auto_off = raw.get("auto_off_at")
        return cls(
            token=token if isinstance(token, str) and token else new_token(),
            password=password if isinstance(password, str) and password else new_password(),
            allow_write=bool(raw.get("allow_write", False)),
            auto_off_at=auto_off if isinstance(auto_off, str) else None,
            sessions=devices,
        )


_UNRESTRICTED = (
    "remote: could not restrict %s to your account — other users on this machine "
    "may be able to read %s"
)


class Runtime:
    """The server's mutable state: ``remote.json``, the live sockets, the audit log.

    Shared between the uvicorn thread and whoever called :func:`start_remote_server` (the
    TUI's thread), so every mutation takes the lock. ``allow_write`` is never
    flipped on here — only :meth:`set_allow_write` does, on an explicit call.
    """

    def __init__(self, state_path: Path, audit_path: Path) -> None:
        self._state_path = state_path
        self._audit_path = audit_path
        self._lock = threading.RLock()
        self._closers: dict[str, set[Callable[[], None]]] = {}
        self._disk: bytes | None = None
        """Digest of the file's bytes as this process last wrote or read them.

        A content fingerprint, not ``(mtime_ns, size)``: the modal coder measured
        195 of 200 same-size rewrites landing inside one mtime tick on this WSL2
        filesystem, so a regenerated passphrase of equal length went unnoticed.
        The file is a few hundred bytes; hashing it costs about what the stat did.
        """
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
        half-written file keeps the state in hand and is retried on the next change.

        What applying the file means: switches and password replace; sessions
        the file no longer lists are revoked here too (cookie gone, websockets
        closed with 4401); ``last_seen`` keeps the newer of memory and disk.
        """
        signature = self._signature()
        with self._lock:
            if signature is None or signature[0] == self._disk:
                return False
            digest, data = signature
            try:
                raw = json.loads(data.decode("utf-8"))
            except ValueError:
                return False
            if not isinstance(raw, dict):
                return False
            self.reads += 1
            self._disk = digest
            incoming = _State.from_json(raw)
            known = {device.sid: device for device in self._state.sessions}
            for device in incoming.sessions:
                previous = known.get(device.sid)
                if previous is not None and previous.last_seen > device.last_seen:
                    device.last_seen = previous.last_seen
            kept = {device.sid for device in incoming.sessions}
            self._state = incoming
            for sid in [sid for sid in known if sid not in kept]:
                self._close_sockets(sid)
            return True

    def _load_state(self) -> _State:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = None
        state = (
            _State.from_json(raw) if isinstance(raw, dict) else _State(new_token(), new_password())
        )
        self._write_state(state)
        return state

    def _write_state(self, state: _State) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(state.state_json(), indent=2).encode("utf-8")
        # The token, the passphrase and every device's cookie, so written as
        # core.credentials writes secrets: a temp of this write's own, created 0600
        # and restricted to this account while still EMPTY (on NTFS, the DACL the
        # rename carries over), then renamed over the target with the Windows
        # contention retry. A shared `remote.json.tmp` written under the umask and
        # chmodded afterwards held them 0644 until the chmod, and two writers
        # collided on its name. Bytes, so the digest below is of what is on disk.
        restricted = write_replacing(self._state_path, encoded, owner_only=True)
        if not restricted and not self._said_unrestricted:
            self._said_unrestricted = True  # once: the flush rewrites the file every 30 s
            log.warning(_UNRESTRICTED, self._state_path, "the password and device cookies")
        # Our own write, by content: the next check finds these exact bytes and
        # skips the parse; a sibling process writing the same size in the same
        # mtime tick is still seen, because its bytes differ.
        self._disk = self._state_digest(encoded)

    def _save_state(self) -> None:
        with self._lock:
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
        return _same(supplied, self.token)

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
        with self._lock:
            self.reload_if_changed()
            self._state.allow_write = bool(enabled)
            self._save_state()

    def set_auto_off(self, at: datetime | None) -> None:
        with self._lock:
            self.reload_if_changed()
            self._state.auto_off_at = at.isoformat(timespec="seconds") if at else None
            self._save_state()

    def regenerate_password(self) -> str:
        """A new password; every unlocked device is dropped with the old one."""
        with self._lock:
            self.reload_if_changed()
            self._state.password = new_password()
            for device in list(self._state.sessions):
                self._drop(device.sid)
            self._save_state()
            return self._state.password

    # -- sessions --

    def unlock_device(self, password: str, ua: str) -> str | None:
        """A new session id when ``password`` is right, else ``None``."""
        with self._lock:
            self.reload_if_changed()
            if not _same(password, self._state.password):
                return None
            sid = new_token()
            stamp = _stamp()
            self._state.sessions.append(Device(sid, ua[:200], stamp, stamp))
            self._save_state()
            return sid

    def device_for_cookie(self, sid: str | None) -> Device | None:
        """The device behind a cookie, its ``last_seen`` refreshed; ``None`` when invalid."""
        if not sid:
            return None
        self.reload_if_changed()
        with self._lock:
            for device in self._state.sessions:
                if _same(sid, device.sid):
                    device.last_seen = _stamp()
                    return device
        return None

    def device_rows(self) -> list[dict[str, str]]:
        """Every unlocked device as ``{sid, ua, first_seen, last_seen}`` (§4-F)."""
        self.reload_if_changed()
        with self._lock:
            return [device.device_json() for device in self._state.sessions]

    def _close_sockets(self, sid: str) -> None:
        for close in self._closers.pop(sid, set()):
            try:
                close()
            except Exception:  # a socket already gone must not stop the revoke
                log.debug("remote: closing a websocket on revoke failed", exc_info=True)

    def _drop(self, sid: str) -> bool:
        before = len(self._state.sessions)
        self._state.sessions = [d for d in self._state.sessions if d.sid != sid]
        self._close_sockets(sid)
        return len(self._state.sessions) != before

    def revoke_device(self, sid: str) -> bool:
        """Drop the cookie session and close its websockets; ``True`` if it existed."""
        with self._lock:
            self.reload_if_changed()
            dropped = self._drop(sid)
            if dropped:
                self._save_state()
            return dropped

    def flush_last_seen(self) -> None:
        """Persist ``last_seen`` (called on a timer, not per request).

        Another process's change lands first: a flush that wrote memory over a
        fresher file would undo the very ``allow-write on`` this is about.
        """
        with self._lock:
            self.reload_if_changed()
            self._save_state()

    def register_socket(self, sid: str, close: Callable[[], None]) -> None:
        with self._lock:
            self._closers.setdefault(sid, set()).add(close)

    def unregister_socket(self, sid: str, close: Callable[[], None]) -> None:
        with self._lock:
            sockets = self._closers.get(sid)
            if sockets:
                sockets.discard(close)
                if not sockets:
                    del self._closers[sid]

    # -- audit --

    def audit(self, sid: str, endpoint: str, summary: str) -> None:
        """``ts sid endpoint summary`` — one line per write that went through (§4-E).

        A line names its device by ``sid``, which IS that device's cookie, so the
        log is owner-only before it holds one: created empty at 0600, then
        restricted to this account (on NTFS, where the bits protect nothing, the
        DACL), the order ``core.atomic`` restricts a temp in. Appended to in text
        mode and chmodded afterwards, the first line sat under the umask until the
        chmod, and Windows never got anything but the DACL its directory hands down.
        """
        line = f"{_stamp()} {sid} {endpoint} {summary}\n".encode()
        with self._lock:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.close(os.open(self._audit_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            except FileExistsError:
                pass
            else:
                if not restrict_to_owner(self._audit_path):
                    log.warning(_UNRESTRICTED, self._audit_path, "the device cookies it records")
            # Binary, so a line ends in "\n" on Windows too.
            with self._audit_path.open("ab") as handle:
                handle.write(line)


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
    """``keys`` as tmux key names, or :class:`RequestError` 400 ``invalid_key``.

    Today: a list of strings, the check ``send-keys`` always made. The allowlist
    of names the pad actually sends replaces this body (SPEC §2.1); every caller
    already goes through here, so none has to change when it does.
    """
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        raise RequestError(400, "invalid_key", "'keys' must be a list of tmux key names")
    return list(keys)


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
    """The four keys every pane response has carried since day one (§4-D)."""
    return {
        "rows": capture.lines,
        "cursor": [capture.facts.cursor_x, capture.facts.cursor_y],
        "width": capture.facts.width,
        "height": capture.facts.height,
    }


def _live_panes(label: str, project: str | None = None, history: int = 0) -> dict[str, object]:
    """One pane frame: the live screen, or scrollback and the screen together (§4-L).

    ``history`` of 0 takes the SAME call today took and returns the SAME four
    keys, so the live stream and every existing client are untouched — the
    history keys appear only when history was asked for.
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
    ``remote-audit.log`` is line-oriented (``ts sid endpoint summary``) and this
    is the first caller-controlled string to reach it, so a name carrying a
    newline would let an authenticated device forge an audit line — and an
    authenticated device is precisely who the trail exists to hold to account.
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


def live_writes() -> Writes:
    """The write endpoints over the services the CLI commands call, then the agent actions."""
    from aisquare.services import remote_actions

    def task_claim(body: dict[str, Any]) -> tuple[dict[str, object], str]:
        from aisquare.services import team as team_service

        task = team_service.claim_task(
            _required(body, "ref"), session_ref=_optional_ref(body, "as")
        )
        return {"task": task.model_dump(mode="json")}, f"claimed {task.id}"

    def task_done(body: dict[str, Any]) -> tuple[dict[str, object], str]:
        from aisquare.services import team as team_service

        task = team_service.finish_task(
            _required(body, "ref"),
            note=_optional_ref(body, "note"),
            session_ref=_optional_ref(body, "as"),
        )
        return {"task": task.model_dump(mode="json")}, f"done {task.id}"

    def write_note(body: dict[str, Any]) -> tuple[dict[str, object], str]:
        """A note on a project's board: ``project``'s, or the current one's without it.

        The board resolves from the project's root exactly as ``asq note`` run
        there would; with ``as``, the session's own board still wins (the CLI's
        rule), so a note posted as an agent lands where that agent reads.
        """
        from aisquare.services import team as team_service

        project = _optional_ref(body, "project")
        event = team_service.add_note(
            _required(body, "text"),
            session_ref=_optional_ref(body, "as"),
            task_ref=_optional_ref(body, "task"),
            to_role=_optional_ref(body, "to"),
            kind=_optional_ref(body, "kind") or "note",
            cwd=None if project is None else _resolve_project(project).root,
        )
        return {
            "event": event.as_envelope().model_dump(mode="json")
        }, f"{event.kind} seq={event.seq}"

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
        from aisquare.core.store import store_session
        from aisquare.core.workspace import find_project_root, project_id_for
        from aisquare.models import ProjectInfo

        path = Path(_required(body, "path")).expanduser()
        if not path.is_dir():
            raise RequestError(400, "invalid", f"{path} is not a directory")
        root = find_project_root(path)
        project = ProjectInfo(id=project_id_for(root), root=root, linked_repos=[])
        with store_session() as store:
            store.ensure_project(project)
        return {"project": project.model_dump(mode="json")}, f"added {project.id} {root}"

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
        from aisquare.core.store import store_session
        from aisquare.services import fleet as fleet_service

        label = _required(body, "agent")
        text = _literal(body, "text")
        keys = None if body.get("keys") is None else check_remote_key_names(body["keys"])
        enter = bool(body.get("enter", False))
        if not text and not keys and not enter:
            raise RequestError(400, "invalid", "give 'text', 'keys' or 'enter'")
        target = _resolve_project(_optional_ref(body, "project"))
        with store_session() as store:
            agent = store.fleet_agent_by_label(target.id, label, live_only=True)
        if agent is None:
            raise NoSuchAgent(f"no live agent {label!r} in {target.root.name or target.id}")
        server = fleet_service.server_for(agent.tmux_socket)
        if text:
            server.send_literal(agent.pane_id, text)
        if keys:
            server.send_keys(agent.pane_id, *keys)
        if enter:
            server.send_keys(agent.pane_id, "Enter")
        summary = (
            f"{label}@{target.id} text={len(text or '')}ch keys={_audit_keys(keys)} enter={enter}"
        )
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
    """``UNLOCK_LIMIT`` attempts per ``UNLOCK_WINDOW_SECONDS`` per client, then 429."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._attempts: dict[str, deque[float]] = {}

    def allow(self, client: str) -> bool:
        now = self._clock()
        window = self._attempts.setdefault(client, deque())
        while window and now - window[0] >= UNLOCK_WINDOW_SECONDS:
            window.popleft()
        if len(window) >= UNLOCK_LIMIT:
            return False
        window.append(now)
        return True


class _Cache:
    """One snapshot per kind per tick, however many sockets are open."""

    def __init__(self, ttl: float) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._values: dict[str, tuple[float, object]] = {}

    def cached_snapshot(self, kind: str, compute: Snapshot) -> object:
        with self._lock:
            hit = self._values.get(kind)
            if hit is not None and time.monotonic() - hit[0] < self._ttl:
                return hit[1]
            value = compute()
            self._values[kind] = (time.monotonic(), value)
            return value


def _client_of(scope: Any) -> str:
    """The client's address — ngrok's ``X-Forwarded-For`` first, the socket peer otherwise."""
    for name, value in scope.get("headers") or []:
        if name == b"x-forwarded-for":
            first = str(bytes(value).decode("latin-1").split(",")[0].strip())
            if first:
                return first
    client = scope.get("client")
    return str(client[0]) if client else "unknown"


def _forwarded_https(request: Request) -> bool:
    """Whether the browser reached us over TLS — ngrok's ``X-Forwarded-Proto`` says."""
    proto = request.headers.get("x-forwarded-proto", "")
    return proto.split(",")[0].strip().lower() == "https"


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
    """Gate 2: Remote's auto-off deadline has not passed.

    Once it has, every request is answered exactly as a wrong token is. The
    deadline itself is not checked here yet (SPEC §2.5), so nothing is refused.
    """
    return True


def remote_gate_origin(scope: Any) -> bool:
    """Gate 3: a write or a socket comes from the remote page's own origin.

    Asked for every method but GET and HEAD, and for every handshake. The
    ``Origin`` rule is not applied here yet (SPEC §2.8), so nothing is refused.
    """
    return True


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
    port: int | None = None
    """The port the app serves on, when its caller said (``build_app(port=)``)."""
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
        except ValueError:
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
        ``X-Forwarded-Proto`` are whatever the sender wrote (SPEC §5.8).
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
            close(WS_CLOSE_REPLACED)

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
                return self.kit_refuse(exc.status, exc.error, exc.message)
            except LookupError as exc:
                return self.kit_refuse(404, "not_found", str(exc))

        async def kit_endpoint(request: Request) -> Response:
            try:
                device = self.kit_device(request)
                if write_gated and not self.kit_write_allowed():
                    raise RequestError(403, "read_only", READ_ONLY_REASON)
                body: dict[str, Any] = {}
                if request.method not in ("GET", "HEAD"):
                    body = await self.kit_json_object(request)
                request_id = _ledger_request_id(body) if write_gated else None
            except RequestError as exc:
                return self.kit_refuse(exc.status, exc.error, exc.message)
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
            log.warning("remote: %s failed; serving without it", starter.__name__, exc_info=True)
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


def build_app(
    runtime: Runtime,
    *,
    sources: Sources | None = None,
    writes: Writes | None = None,
    dist_dir: Path | None = None,
    tick: float = TICK_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    port: int | None = None,
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
        from starlette.websockets import WebSocketDisconnect
    except ImportError as exc:  # pragma: no cover - exercised only in a base install
        raise RemoteUnavailable(f"the remote extra is not installed — {INSTALL_HINT}") from exc

    from aisquare.services import remote_actions, remote_needs, remote_push

    reads = sources or live_sources()
    handlers = (writes or live_writes()).handlers
    dist = (dist_dir or remote_dist_dir()).resolve()
    limiter = _RateLimiter(clock)
    cache = _Cache(ttl=tick * 0.9)
    kit = RemoteKit(runtime, tick=tick, port=port)

    def cookie_path(request: Request) -> str:
        return f"/r/{request.path_params['token']}"

    async def snapshot(kind: str, compute: Snapshot) -> object:
        return await asyncio.to_thread(cache.cached_snapshot, kind, compute)

    def guarded(
        kind: str, compute: ProjectSource, *, scoped: bool = True
    ) -> Callable[[Request], Any]:
        """A cached read. ``?project=`` picks the project (``scoped``) and is part of the
        cache key, so a read of one project is never answered from another's snapshot
        (SPEC §7.5); an unknown project is a 404 shaped like an unknown agent."""

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

    async def unlock_endpoint(request: Request) -> Response:
        if not limiter.allow(_client_of(request.scope)):
            return _json_error(429, "too_many_attempts", "5 attempts a minute — wait")
        try:
            body = await kit.kit_json_object(request)
        except RequestError:
            body = {}
        password = body.get("password")
        if not isinstance(password, str):
            return _json_error(400, "invalid", 'send {"password": "..."}')
        sid = runtime.unlock_device(password, request.headers.get("user-agent", ""))
        if sid is None:
            return _json_error(401, "wrong_password")
        response = JSONResponse({"ok": True})
        response.set_cookie(
            COOKIE,
            sid,
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

        Not built yet: the answer is the one for a Remote with no deadline.
        """
        return kit.kit_refuse(409, "no_auto_off", "Remote has no auto-off deadline to extend")

    async def devices_list_endpoint(request: Request) -> Response:
        device = kit.kit_device(request)
        rows: list[dict[str, object]] = [
            {"id": row["sid"], **row, "current": row["sid"] == device.id}
            for row in runtime.device_rows()
        ]
        return JSONResponse(rows)

    async def devices_delete_endpoint(request: Request) -> Response:
        """Sign this device out, always; revoke ANOTHER device only while writes are on.

        A read-only phone could otherwise sign every other phone out, the
        owner's included, which is a change to who can reach the fleet.
        """
        device = kit.kit_device(request)
        device_id = request.path_params["device_id"]
        own = device_id == device.id
        if not own and not kit.kit_write_allowed():
            return kit.kit_refuse(403, "read_only", READ_ONLY_REASON)
        if not runtime.revoke_device(device_id):
            return kit.kit_refuse(404, "not_found", "no such device")
        kit.kit_audit(device, "devices/revoke", "self" if own else device_id)
        return JSONResponse({"ok": True, "id": device_id})

    async def panes(request: Request) -> Response:
        agent = request.path_params["agent"]
        project = request.query_params.get("project") or None
        try:
            history = _history_param(request.query_params.get("history"))
        except ValueError as exc:
            return _json_error(400, "invalid", str(exc))
        try:
            payload = await asyncio.to_thread(reads.panes, agent, project, history)
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
            status, payload = exc.status, _error_body(exc.error, exc.message)
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
        rel = request.path_params.get("path", "")
        if rel:
            candidate = (dist / rel).resolve()
            if candidate.is_relative_to(dist) and candidate.is_file():
                return FileResponse(candidate, headers={"cache-control": _cache_control(rel)})
        if rel and not _is_navigation(rel, request.headers.get("accept", "")):
            # A file was asked for and there is no such file. Saying so is the
            # whole point: the SPA document under a .js name is a boot failure
            # with no error, and the 200 hides which build is actually installed.
            return _json_error(404, "not_found", f"no such file in the built page: {rel}")
        index = dist / "index.html"
        if index.is_file():
            return FileResponse(index, headers={"cache-control": INDEX_CACHE_CONTROL})
        return _json_error(
            404,
            "no_dist",
            f"no built page at {dist} — build aisquare-remote and copy its dist/ there, "
            "or pass --dist",
        )

    async def stream(websocket: WebSocket) -> None:
        """``/ws``: every tick, each frame that changed (SPEC §1.6).

        In order: ``board``, ``fleet``, ``remote``, then ``needs_you`` and
        ``action``, then the ``heartbeat`` (every ``heartbeat`` seconds, changed
        or not, never on the first tick), then one ``pane`` frame per
        subscription. Pane subscriptions are ``(project, label)``: the same
        label in two projects is two agents, and a frame names the project its
        subscription named.
        """
        device = kit.kit_device(websocket)  # the gate refused a socket without one
        await websocket.accept()
        sid = device.sid
        loop = asyncio.get_running_loop()
        panes_wanted: dict[tuple[str, str], None] = {}
        """``(project ref, label)`` per pane subscription, oldest first; ``""`` is the CURRENT
        project. A dict for its order: frames follow the order subscriptions came in."""
        fleet_project: str | None = None
        """``None`` = the CURRENT project; a ``{subscribe_fleet: "<project>"}`` text frame
        picks another one's ``fleet`` frames (``""``/``null`` returns). The frame shape
        does not change, only WHICH project's ``fleet ls`` payload fills it."""
        board_project: str | None = None
        """The same, for ``board`` frames and ``{subscribe_board: "<project>"}``."""
        last: dict[tuple[str, ...], str] = {}
        next_heartbeat = time.monotonic() + heartbeat
        first_tick = True

        async def close_with(code: int) -> None:
            with contextlib.suppress(Exception):
                await websocket.close(code=code)

        def closer(code: int) -> None:
            loop.call_soon_threadsafe(lambda: loop.create_task(close_with(code)))

        def revoked() -> None:
            closer(WS_CLOSE_UNAUTHORIZED)

        async def send_frame(
            kind: str, payload: object, *, agent: str | None = None, project: str | None = None
        ) -> None:
            frame: dict[str, object] = {"type": kind, "payload": payload, "ts": _stamp()}
            if agent is not None:
                frame["agent"] = agent
            if project is not None:
                frame["project"] = project
            await websocket.send_text(json.dumps(frame))

        async def push_if_changed(
            key: tuple[str, ...],
            kind: str,
            payload: object,
            *,
            agent: str | None = None,
            project: str | None = None,
        ) -> None:
            encoded = json.dumps(payload, sort_keys=True)
            if last.get(key) != encoded:
                last[key] = encoded
                await send_frame(kind, payload, agent=agent, project=project)

        async def tick_once() -> None:
            nonlocal next_heartbeat, first_tick
            board_ref = board_project
            try:
                payload = await snapshot(f"board:{board_ref or ''}", lambda: reads.board(board_ref))
                await push_if_changed(("board", board_ref or ""), "board", payload)
            except Exception as exc:
                log.debug("remote: board frame skipped: %s", exc)
            fleet_ref = fleet_project
            try:
                payload = await snapshot(f"fleet:{fleet_ref or ''}", lambda: reads.fleet(fleet_ref))
                await push_if_changed(("fleet", fleet_ref or ""), "fleet", payload)
            except Exception as exc:
                log.debug("remote: fleet frame skipped: %s", exc)
            await push_if_changed(("remote",), "remote", runtime.remote_json())
            for kind, payload in remote_needs.needs_ws_frames(kit):
                await push_if_changed((kind,), kind, payload)
            actions = kit.ledger.ledger_recent(device.id)
            if actions:
                await push_if_changed(("action",), "action", {"actions": actions})
            now = time.monotonic()
            if first_tick:
                first_tick = False
            elif now >= next_heartbeat:
                next_heartbeat = now + heartbeat
                scanned = remote_needs.needs_scanned_iso(kit)
                await send_frame("heartbeat", {"needs_scanned_at": scanned})
            for project, label in list(panes_wanted):
                try:
                    # §4-L: history is a FETCH, live stays a stream — 0 keeps
                    # this frame exactly the §4-D shape it has always had.
                    payload = await loop.run_in_executor(
                        kit.kit_pane_pool(), reads.panes, label, project or None, 0
                    )
                except Exception as exc:
                    payload = {"rows": [], "width": 0, "height": 0, "error": str(exc)}
                await push_if_changed(
                    ("pane", project, label), "pane", payload, agent=label, project=project or None
                )

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
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                ref = message.get("project")
                project = ref if isinstance(ref, str) else ""
                label = message.get("subscribe")
                if isinstance(label, str) and label:
                    if (project, label) in panes_wanted or len(
                        panes_wanted
                    ) < WS_PANE_SUBSCRIPTIONS_MAX:
                        panes_wanted[(project, label)] = None
                        last.pop(("pane", project, label), None)
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
                    last.pop(("fleet", fleet_project or ""), None)
                target = message.get("subscribe_board", False)
                if target is None or isinstance(target, str):
                    board_project = target or None
                    last.pop(("board", board_project or ""), None)

        runtime.register_socket(sid, revoked)
        kit.kit_socket_opened(device.id, closer)
        reading = asyncio.ensure_future(reader())
        try:
            while not reading.done():
                if runtime.device_for_cookie(sid) is None:
                    await close_with(WS_CLOSE_UNAUTHORIZED)
                    break
                await tick_once()
                await asyncio.wait([reading], timeout=tick)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            log.debug("remote: stream for %s ended: %s", device.id, exc)
        finally:
            kit.kit_socket_closed(device.id, closer)
            runtime.unregister_socket(sid, revoked)
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


class _Server:
    """uvicorn in a daemon thread, stopped by flipping ``should_exit``."""

    def __init__(self, app: Any, port: int) -> None:
        import uvicorn

        self.port = port
        config = uvicorn.Config(app, host=BIND, port=port, log_level="warning", ws="auto")
        self._server = uvicorn.Server(config)
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
_flusher: threading.Timer | None = None


def _page_missing(dist_dir: Path | None) -> str | None:
    """``None`` when the directory ``build_app`` would serve has an ``index.html``.

    Checked up front by :func:`start_remote_server` and :func:`run_foreground`, not by
    :func:`build_app` itself: an explicit ``--dist``/``dist_dir`` that turns out
    to be wrong is still a per-request 404 (``test_missing_dist_is_a_404_...``),
    because the caller named that path on purpose and may still be building it.
    What must never happen silently is the DEFAULT — ``dist_dir=None`` falling
    back to :func:`remote_dist_dir`, which nothing populates until
    :func:`install_page` runs — so a fresh machine's first ``R`` press gets a
    sentence instead of a server that answers every request with nothing.
    """
    dist = (dist_dir or remote_dist_dir()).resolve()
    return None if (dist / "index.html").is_file() else NO_PAGE_HINT


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
        app = build_app(state, dist_dir=dist_dir)
        server = _Server(app, port)
        server.start_serving()
        _server = server
    _schedule_flush()
    return state.connection_info(port)


def stop_remote_server() -> None:
    """Stop the background server (no-op when it is not running)."""
    global _server, _flusher
    with _lock:
        server, _server = _server, None
        flusher, _flusher = _flusher, None
    if flusher is not None:
        flusher.cancel()
    if server is not None:
        server.stop_serving()
    if _runtime is not None:
        _runtime.flush_last_seen()


def remote_server_status() -> dict[str, object]:
    """``{running, sessions:[{sid, ua, first_seen, last_seen}]}`` (PLAN §4-F)."""
    with _lock:
        running = _server is not None and _server.running
    return {"running": running, "sessions": runtime().device_rows()}


def revoke_remote_device(sid: str) -> bool:
    """Drop a device's cookie session and close its websockets."""
    return runtime().revoke_device(sid)


def set_allow_write(enabled: bool) -> None:
    """Flip the write gate — the ONLY way it turns on; the default is off."""
    runtime().set_allow_write(enabled)


def regenerate_password() -> str:
    """A fresh password; every unlocked device has to unlock again."""
    return runtime().regenerate_password()


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


def _schedule_flush() -> None:
    """Persist ``last_seen`` every 30 s while serving, instead of once per request."""
    global _flusher

    def flush_and_rearm() -> None:
        with _lock:
            serving = _server is not None and _server.running
        if _runtime is not None:
            _runtime.flush_last_seen()
        if serving:
            _schedule_flush()

    with _lock:
        if _flusher is not None:
            _flusher.cancel()
        _flusher = threading.Timer(30.0, flush_and_rearm)
        _flusher.daemon = True
        _flusher.start()


def run_foreground(dist_dir: Path | None = None, port: int = DEFAULT_PORT) -> None:
    """``asq remote serve``: block in this thread until Ctrl-C."""
    problem = _remote_dependency_error()
    if problem is not None:
        raise RemoteUnavailable(problem)
    page_problem = _page_missing(dist_dir)
    if page_problem is not None:
        raise NoRemotePage(page_problem)
    import uvicorn

    app = build_app(runtime(), dist_dir=dist_dir)
    uvicorn.run(app, host=BIND, port=port, log_level="warning", ws="auto")


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
    "RemoteError",
    "RemoteInfo",
    "RemoteUnavailable",
    "RequestError",
    "Runtime",
    "Sources",
    "Writes",
    "build_app",
    "build_local_url",
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
