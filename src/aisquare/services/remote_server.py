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
task and entry dumps), so nobody invents a field here; the board carries its newest
:data:`BOARD_EVENTS` events where the CLI's glance has five. Each takes ``?project=``
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

**The stream** sends ``remote``, then ``needs_you`` and ``action``, each only when it
changed, a ``heartbeat`` every :data:`HEARTBEAT_SECONDS` changed or not, then one
``pane`` frame per ``(project, label)`` subscription when its pane changed. A socket
that asked with ``subscribe_fleet`` gets ``fleet`` frames too, a project's ``fleet
ls``, and one that asked with ``subscribe_board`` gets ``board`` frames, both ahead of
the rest: the board's events and the sessions they name (:func:`remote_board_frame`),
or why the board could not be read (:func:`remote_board_unread`), each naming the
project its subscription named, as a ``pane`` frame does. A tick waits a tick at most
for the snapshots it reads: one still being read sends its frame on a later tick, and
holds back no other.

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
import contextvars
import dataclasses
import errno
import functools
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
from typing import TYPE_CHECKING, Any, NoReturn

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
    import asyncio
    import socket
    from concurrent.futures import Executor, Future, ThreadPoolExecutor
    from types import FrameType, TracebackType

    import uvicorn
    from starlette.requests import HTTPConnection, Request
    from starlette.responses import Response
    from starlette.routing import Route
    from starlette.websockets import WebSocket

    from aisquare.core.tmux import Capture, PaneFacts, TmuxServer
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
WRITE_WORKERS = 8
"""Threads in the pool every write handler runs on (:meth:`RemoteKit.kit_write_pool`): keys
waiting out an action's lock, and the actions themselves, which take seconds."""
WRITE_WAITING_PER_DEVICE = 64
"""Writes one device may have waiting for a thread of the write pool; one more is 409
``busy`` (:meth:`RemoteKit.kit_run_write`). A restart or a switch holds a thread
for 20 to 40 s, and the pool's queue had no end: a device could bank writes behind them by
the hundred, to run long after it sent them, ahead of every other device's (sweep 2 of
#243). The page sends one agent's keys one at a time, and an action holds its agent's
lock, so a phone never comes near this; a burst of taps at a busy agent's pad does not
either (they are refused 409 ``busy`` within 2 s)."""
HEARTBEAT_SECONDS = 10.0
"""How often a socket gets a ``heartbeat`` frame, changed or not, so the page can tell a quiet
fleet from a dead link (the default of ``build_app(heartbeat=)``)."""
CACHE_KINDS_MAX = 64
"""Snapshots the read cache keeps at once, however many ``?project=`` spellings are asked for
within one tick (:class:`_Cache`): a phone reads a handful, and one pane frame for each pane
its sockets watch, up to :data:`WS_PANE_SUBSCRIPTIONS_MAX` a socket. Past the cap the oldest
go first, which costs a read or a capture again within the tick, never a wrong answer."""

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

REMOTE_SERVER_NEEDS = ("starlette", "uvicorn", "websockets")
"""What the server cannot start without (:func:`_remote_dependency_error`)."""
REMOTE_EXTRA = (*REMOTE_SERVER_NEEDS, "cryptography")
"""What the ``remote`` extra installs: the server's three, and what Web Push needs."""

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
SEND_KEYS_LOCK_WAIT_SECONDS = 2.0
"""How long a send-keys waits for its agent's action lock (:func:`remote_agent_lock`), counted
from when the request reached the server, its wait for a thread of the write pool included
(:func:`_remote_keys_turn`). Keys tapped in a burst, or sent again together
after a reconnect, wait out the milliseconds each other's tmux calls take; an action holds
the lock for seconds (an interrupt's wait for the prompt, a stop's grace, a restart), and keys
that would land in the middle of it are 409 ``busy`` instead."""
NOTE_TEXT_MAX = 8_000
NOTE_TO_MAX = 200
"""The longest ``to`` a note may name, a role or a label: what the page's composer takes. The
board keeps it with the event, and every board read and frame carries it."""
NOTE_KINDS = frozenset({"note", "decision", "question", "result"})
"""The kinds a phone may post. The others (``attention``, ``limited``, ``agent_exited``,
``switched``…) are the fleet's own reports, which wake the manager or set an agent's state."""
PROJECT_ADD_PATH_MAX = 4_096
NUL_IN_A_PATH = "{field!r} holds a NUL byte, which no file's name or path holds"
"""The refusal of a path or a project ref the system would not look up (``field`` named)."""

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
REMOTE_BIDI_CONTROLS = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
"""The bidi embeddings, overrides and isolates. Each reorders the text after it wherever
bidi is applied, the phone's page and some terminals: ``approve`` then an override and
``deleted`` reads as something else than was written. The marks (U+200E, U+200F) only
place the neutral characters beside them, and a line of right-to-left text keeps them."""
_TEXT_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")
"""What typed ``text`` may not hold: a control character, C0 other than newline, DEL or C1
(:func:`check_remote_text`). A carriage return is the Enter key, byte for byte, and a tab
the Tab key."""
_PASTED_CONTROL = re.compile(
    "[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\x7f-\\x9f" + "".join(sorted(REMOTE_BIDI_CONTROLS)) + "]"
)
"""What a paste (a tell, or a note an agent's fresh replacement is handed) may not hold: the
same, but for the tab and the carriage return, which inside a bracketed paste are a tab and
a line break of the message and press nothing; and a bidi control
(:data:`REMOTE_BIDI_CONTROLS`), since a tell may be filed as a note, and ``aisquare board``
prints a note's text as it came, past Rich, which strips only BEL, BS, VT, FF and CR."""
_TEXT_CONTROL_KEYS = {
    "\x03": "C-c",
    "\x04": "C-d",
    "\t": "Tab",
    "\x0c": "C-l",
    "\r": "Enter",
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


class RemoteAlreadyOn(RemoteError):
    """Another process serves Remote from this home already (:func:`_claim_remote_home`)."""


class RemoteWindingDown(RemoteError):
    """The Remote this process turned off last still finishes a phone's write, or answers a
    request (:func:`start_remote_server`)."""


class RemoteOffIncomplete(RemoteError):
    """``serve``'s auto-off turned Remote off, but could not do all of it: the devices still
    signed in, or the deadline still in ``remote.json`` (:func:`run_foreground`)."""


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
    return _iso_seconds(_remote_now())


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
_UNCOUNTED = "remote: a wrong passphrase was counted in memory only: %s could not be written (%s)"
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


_WRITE_ARRIVED: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "asq_remote_write_arrived", default=None
)
"""When the write a handler runs for reached the server (``time.monotonic``): a wait it makes
counts from then, not from when a thread of the write pool was free to run it."""

_STATE_CHECKED: contextvars.ContextVar[Runtime | None] = contextvars.ContextVar(
    "asq_remote_state_checked", default=None
)
"""The runtime whose ``remote.json`` this request, or this socket's tick, has checked already
for another process's change (:meth:`Runtime.remote_state_checked`)."""


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
        self._audit_lock = threading.Lock()
        """One audit line at a time, the log made and restricted before its first
        (:meth:`audit`). Never :attr:`_lock`: that restriction is ``icacls`` on Windows,
        another process, and a slow one held up every request's gate and every socket's
        tick, for a line that touches nothing in memory."""
        self._writing = threading.RLock()
        """One read-modify-write of ``remote.json`` at a time in this process, held while
        ``remote.json.lock`` is waited for (:meth:`_state_file_lock`). Taken before
        :attr:`_lock`, never while holding it."""
        self._closers: dict[str, set[Callable[[int], None]]] = {}
        """Each device's live sockets, by device id, as closers that take the close code."""
        self._file_lock_depth = 0
        self._writer: int | None = None
        """The thread in :meth:`_state_file_lock`, while one is: what it writes must start from
        the file, so its check is never one a request made earlier (:meth:`reload_if_changed`)."""
        self._unpublished: bytes | None = None
        """What the read-modify-write in hand decided to write (:meth:`_write_state`), until
        its outermost :meth:`_state_file_lock` publishes it."""
        self._unpublished_undo: list[Callable[[], None]] = []
        """How to put memory back should that write fail (:meth:`_write_state`'s ``undo``)."""
        self._disk: bytes | None = None
        """The file's bytes as this process last wrote or read them.

        The content, not ``(mtime_ns, size)``: the modal coder measured 195 of 200
        same-size rewrites landing inside one mtime tick on this WSL2 filesystem, so
        a regenerated passphrase of equal length went unnoticed. And the bytes, not a
        hash of them: they hold the passphrase, and a blake2b of it read as a password
        kept under a fast hash (CodeQL ``py/weak-sensitive-data-hashing``). The bytes
        are as exact, hold nothing :attr:`_state` does not, and a few hundred of them
        compare for less than the hash cost.
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
        self._going_off = False
        """Remote is turning off, from the revoke on (:meth:`remote_going_off`), until it is
        started again (:meth:`remote_coming_on`); memory only."""
        self._state = self._load_state()

    # -- persistence --

    def _signature(self) -> bytes | None:
        """The file's bytes right now, or ``None`` when it cannot be read."""
        try:
            return self._state_path.read_bytes()
        except OSError:
            return None

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
        and no socket tick. Until the rename the bytes this process knows the file
        by stay the old ones, so a reload meanwhile finds nothing new to adopt; and
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
                self._writer = threading.get_ident()
                try:
                    try:
                        with self._lock:
                            yield
                    finally:
                        body, self._unpublished = self._unpublished, None
                        undo, self._unpublished_undo = self._unpublished_undo, []
                        if body is not None:
                            try:
                                self._publish_state(pending, unmade, body)
                            except BaseException:
                                with self._lock:
                                    for step in reversed(undo):
                                        step()
                                raise
                finally:
                    self._file_lock_depth = 0
                    self._writer = None
                    if fd is not None:
                        with contextlib.suppress(OSError):
                            unlock(fd)
                        os.close(fd)

    def reload_if_changed(self) -> bool:
        """Re-read ``remote.json`` if ANOTHER process changed it; ``True`` when it had.

        ``aisquare remote allow-write on``, ``regenerate-password`` and ``revoke``
        run in their own process and write the file; a serving process that only
        trusted memory kept answering with the old switches (measured: 30 s of
        ``allow_write:false`` after the toggle). One small read and a compare of
        its bytes per check is the whole cost; the file is parsed only when its
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

        Inside :meth:`remote_state_checked` the block's first check stands for the
        rest, but never for a read-modify-write's own, under the file lock.
        """
        if _STATE_CHECKED.get() is self and self._writer != threading.get_ident():
            return False
        with self._lock:
            moves = self._disk_moves
        data = self._signature()
        with self._lock:
            if data is None or data == self._disk or moves != self._disk_moves:
                return False
            try:
                raw = json.loads(data.decode("utf-8-sig"))
            except (ValueError, RecursionError):
                return False
            if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
                return False
            self.reads += 1
            self._disk = data
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

    @contextlib.contextmanager
    def remote_state_checked(self) -> Iterator[None]:
        """Check ``remote.json`` for another process's change ONCE for everything inside:
        one request, gates and route, or one tick of a socket.

        Every read of the state checks the file (:meth:`reload_if_changed`), a read and a
        compare each, and one request made three or four of them on the event loop that
        serves every request and socket: the token, the deadline and the cookie's device
        at the gate, then the route's own (``api/remote``, the write gate); and every
        socket three a second (review of #243, round 3). Inside this block the first
        check stands for the rest, so another process's change is still seen by the next
        request and the next tick. A read-modify-write in it still reads the file under
        the file lock: what it writes must start from what is on disk.
        """
        self.reload_if_changed()
        token = _STATE_CHECKED.set(self)
        try:
            yield
        finally:
            _STATE_CHECKED.reset(token)

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
                self._disk = data
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

    def _write_state(self, state: _State, *, undo: Callable[[], None] | None = None) -> None:
        """Have ``remote.json`` replaced with ``state`` when the outermost
        :meth:`_state_file_lock` ends; callers hold it. The last state handed over in
        one read-modify-write is the one written, once.

        ``undo`` puts memory back should that write fail, still under the locks, so no
        flush writes what it took back. Only for a change that is worse in memory alone
        than not made: an unlock's device, whose secret no browser was handed, which
        the panel and every Devices screen listed as signed in, and the next flush saved
        (sweep 2 of #243); or a later deadline the phone was told nothing of. A change
        that is safe in memory alone (a revoke, writes off) keeps none: the gate goes by
        it, written or not (review of #243, round 2).
        """
        if not self._file_lock_depth:
            raise RuntimeError("remote.json is written only under _state_file_lock")
        self._unpublished = _encoded_state(state)
        if undo is not None:
            self._unpublished_undo.append(undo)

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
        collided on its name. Bytes, so what this process knows is what is on
        disk: the next check finds these exact bytes and skips the parse, and a
        sibling process writing the same size in the same mtime tick is still
        seen, because its bytes differ. ``unmade`` is why there is no temp, raised
        now that there is something to write.
        """
        if pending is None:
            assert unmade is not None
            raise unmade
        pending.publish(body)
        with self._lock:
            self._disk = body
            self._disk_moves += 1
        if not pending.restricted and not self._said_unrestricted:
            self._said_unrestricted = True  # once: the flush may rewrite the file every 30 s
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
        """Whether Remote's deadline is behind ``now``, or Remote is turning off
        (:meth:`remote_going_off`): then every request is a 404."""
        if self._going_off:
            return True
        deadline = self.auto_off_deadline()
        return deadline is not None and now >= deadline

    def remote_going_off(self) -> None:
        """Remote is turning off, by its switch or auto-off: until :meth:`remote_coming_on`,
        every request is answered and every socket closed as past the deadline, and no
        unlock makes or renews a device (:meth:`unlock_device`).

        Turning off revokes every device, clears the deadline, and only then stops the
        server, which uvicorn sees at its next tick, and both writes may wait for
        ``remote.json.lock``. An unlock in between made a device the revoke never saw, which
        the flush on the way out saved, its cookie good on the next Remote for 7 days; and
        past the deadline, its clearing opened the gate again for that while (review of
        #243, round 5). Set before the revoke, which takes the file lock after it: an unlock
        that held that lock first made a device the revoke then drops.
        """
        with self._lock:
            self._going_off = True

    def remote_coming_on(self) -> None:
        """A server is starting over this state: what :meth:`remote_going_off` closed opens."""
        with self._lock:
            self._going_off = False

    def _refuse_while_going_off(self) -> None:
        """Under the file lock, as a device is made or renewed: a 404, as the gate answers,
        once Remote is turning off (:meth:`remote_going_off`)."""
        if self._going_off:
            raise RequestError(404, "not_found", LINK_GONE)

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
            state, before = self._state, self._state.auto_off_at
            state.auto_off_at = _iso_seconds(extended)
            self._write_state(state, undo=lambda: setattr(state, "auto_off_at", before))
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
            self._refuse_while_going_off()
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
            state = self._state
            state.devices.append(device)

            def unmade() -> None:
                state.devices = [kept for kept in state.devices if kept is not device]

            self._write_state(state, undo=unmade)
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
            self._refuse_while_going_off()
            self.reload_if_changed()
            device = self._find_device(device_id)
            if device is None or device.device_expired(now):
                return None
            secret = secrets.token_urlsafe(32)
            was = dataclasses.replace(device)

            def unmade() -> None:  # the browser keeps its old cookie, so the device does too
                device.secret_sha256, device.last_seen = was.secret_sha256, was.last_seen
                device.failed_unlocks, device.ua = was.failed_unlocks, was.ua

            device.secret_sha256 = _secret_digest(secret)
            device.last_seen = _iso_seconds(now)
            device.failed_unlocks = 0
            if ua:
                device.ua = _audit_clean(ua, DEVICE_UA_MAX)
            self._write_state(self._state, undo=unmade)
            return secret, device

    def known_device_failed(self, device_id: str) -> bool:
        """Count a wrong passphrase sent with this device's cookie; ``True`` when that
        was the :data:`KNOWN_DEVICE_FAILURES_MAX`-th and the device is revoked: a stolen
        cookie buys at most that many guesses outside the global budget.

        Counted in memory when ``remote.json`` will not write, and said in the log: a
        wrong guess is still a wrong guess, where the write's error made it a bare 500
        that read as the machine's fault (sweep 2 of #243).
        """
        revoked = False
        try:
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
        except OSError as exc:
            log.warning(_UNCOUNTED, self._state_path, exc)
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

        A flush with nothing new writes nothing. It wrote every time, every 30 s
        while Remote was on with no phone even open: an owner-only temp (an
        ``icacls`` run on Windows, ``_writing`` held through it), the same bytes,
        two fsyncs and a rename (sweep 2 of #243). Nothing new is the file's own
        bytes being what memory would write, and no device past its lifetime.
        Compared with the file, not with what this process last wrote or read
        (:attr:`_disk`): a version-1 file written under a running server is never
        adopted, so what this process last wrote still matched memory, and the
        flush must write version 2 back over it.
        """
        if self._flush_needless(self._signature()):
            return
        with self._state_file_lock():
            self.reload_if_changed()
            self.prune_expired_devices(_remote_now())
            if not self._flush_needless(self._signature()):
                self._write_state(self._state)

    def _flush_needless(self, on_disk: bytes | None) -> bool:
        """Whether the file's bytes (:meth:`_signature`) are what memory would write, with no
        device left for a flush to prune."""
        now = _remote_now()
        with self._lock:
            return (
                on_disk is not None
                and on_disk == _encoded_state(self._state)
                and not any(device.device_expired(now) for device in self._state.devices)
            )

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
        DACL), the order ``core.atomic`` restricts a temp in, all under
        :attr:`_audit_lock` alone.
        """
        fields = (
            _audit_clean(device_id, AUDIT_DEVICE_MAX),
            _audit_clean(endpoint, AUDIT_ENDPOINT_MAX),
            _audit_clean(summary, AUDIT_SUMMARY_MAX),
        )
        line = f"{_stamp()} {' '.join(fields)}\n".encode()
        with self._audit_lock:
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
        recent: list[datetime] = []
        try:
            with runtime._state_file_lock():
                runtime.reload_if_changed()
                recent = [*self._recent(now), now]
                runtime._state.unlock_failures = [_iso_seconds(stamp) for stamp in recent]
                runtime._write_state(runtime._state)
        except OSError as exc:  # counted in memory all the same (Runtime.known_device_failed)
            log.warning(_UNCOUNTED, runtime._state_path, exc)
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
Raises :class:`NoSuchAgent` / :class:`NoSuchProject`, and :class:`RequestError` 409
``not_agent`` for a row whose pane id is another agent's now."""
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

    The actions and the quick answers take it without blocking and answer 409
    ``busy`` when it is held: a second stop, restart or quick answer arriving
    while the first still runs would otherwise act on the state the first is in
    the middle of changing. Keys wait for it a moment first
    (:data:`SEND_KEYS_LOCK_WAIT_SECONDS`), since the pad sends taps without
    waiting for each other's answers.
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


def check_remote_text(text: str, *, pasted: bool = False) -> None:
    """Refuse typed ``text`` holding a control character (C0, DEL or C1) other than
    newline: 400 ``invalid``, naming the pad's key for it. A ``pasted`` text (a tell)
    may also hold a tab and a carriage return, and no bidi control (:data:`_PASTED_CONTROL`).

    Text reaches the pane as hex, byte for byte, so a control character in it IS a
    keystroke: ``"\\x03"`` was a Ctrl-C past the double-press guard, ``"\\x1a"`` the
    Ctrl-Z :data:`REMOTE_KEY_NAME` refuses as a key, and the audit line said
    ``text=1ch``, which cannot tell either from a letter. Keys go as ``keys``, where
    the allowlist, the guard and the trail see them by name. A carriage return is the
    Enter key's own byte: ``{"text": "\\r"}`` took a dialog's highlighted option while
    the trail said ``enter=False``, and each line of a CRLF text was a prompt of its own
    (review of #243, round 3). A tab is the Tab key's: Claude Code's prompt takes it as
    a key (an open suggestion accepted), never as a tab of the message, so a pasted
    table row arrived as other text than was sent (review of #243, round 5). Inside a
    tell's bracketed paste both are the message's own, as a newline is.
    """
    found = (_PASTED_CONTROL if pasted else _TEXT_CONTROL).search(text)
    if found is None:
        return
    char = found.group()
    key = _TEXT_CONTROL_KEYS.get(char)
    if char in REMOTE_BIDI_CONTROLS:
        instead = "it reorders how the text after it reads"
    else:
        instead = f"send the pad's {key} key instead" if key else "no key of the pad sends it"
    raise RequestError(400, "invalid", f"'text' holds {_remote_char_named(char)} — {instead}")


def _remote_char_named(char: str) -> str:
    """How a refusal names a character that text may not hold: by its code point, as a
    control character or a bidi control (:data:`REMOTE_BIDI_CONTROLS`)."""
    kind = "the bidi control" if char in REMOTE_BIDI_CONTROLS else "the control character"
    return f"{kind} U+{ord(char):04X}"


def check_note_text(text: str, field: str) -> None:
    """A note's ``field`` as a phone may post it: at most :data:`NOTE_TEXT_MAX` characters
    (413), and no control character but tab, newline and carriage return, and no bidi
    control (400 ``invalid``), which is a tell's rule.

    A note posted ``as`` an agent's session is one of that session's newest board
    entries, and the first prompt of a fresh replacement repeats them
    (``fleet._handoff_prompt``): one bracketed paste into its pane, whose bytes tmux
    before 3.7 pastes as they are. ``"ok\\x1b[201~\\x1a\\r\\x03"`` ended that paste, and
    Ctrl-Z, an Enter and a Ctrl-C followed as keystrokes, past :data:`REMOTE_KEY_NAME`
    and the double-press guard, the next time the agent started fresh: a restart or a
    switch asked to, from a phone or the manager, or one whose transcript was gone
    (review of #243, round 3). A finished task's note is the text of its
    ``task_done`` event, so ``task/done`` holds it to the same rule. A line break
    inside the paste is the note's own, as it is in a tell.

    ``team.event_line`` puts the text on the line ``aisquare board`` prints, as it does
    ``to`` (:func:`check_note_to`), and Rich passes C1 and bidi controls through: a C1
    CSI or OSC (``"\\x9b2J"``, ``"\\x9d52;c;…\\x9c"``) reached a terminal that reads UTF-8
    C1 as controls, as xterm and VTE do, and an override made ``approve`` and ``deleted``
    read in another order. Only the ASCII ones were refused (sweep 3 of #243).
    """
    if len(text) > NOTE_TEXT_MAX:
        raise RequestError(413, "too_large", f"a note is at most {NOTE_TEXT_MAX} characters")
    found = _PASTED_CONTROL.search(text)
    if found is not None:
        raise RequestError(
            400,
            "invalid",
            f"{field!r} holds {_remote_char_named(found.group())} — a note may hold tabs and "
            "line breaks, and no other control character and no bidi control",
        )


def check_note_to(to: str) -> None:
    """A note's ``to`` as a phone may post it: a role or a label, every character one that
    prints (400 ``invalid``).

    The board keeps it with the event as it came, and ``team.event_line`` puts it on
    the line ``asq board`` prints and every agent's team delta repeats: ``manager``
    and an OSC 52 after it set the owner's clipboard from the terminal ``asq board``
    ran in, past Rich, which strips only BEL, BS, VT, FF and CR. The note's text was
    refused the same bytes (review of #243, round 4). A name has no use for a tab, a
    line break, a C1 control or a bidi override, so none is kept, where a text keeps
    its tabs and line breaks (:func:`check_note_text`).
    """
    for char in to:
        if not char.isprintable():
            raise RequestError(
                400,
                "invalid",
                f"'to' holds U+{ord(char):04X}, which does not print — 'to' names a role or a "
                "label",
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
    A path the system refuses to look up is refused as such: a NUL byte's
    ``ValueError`` and the ``RuntimeError`` of a ``~user`` with no home here fell
    to 400 ``write_failed``, the system's own words for a write that never began
    (sweep 3 of #243).
    """
    from aisquare.core.workspace import find_project_root
    from aisquare.services import fleet as fleet_service

    if not isinstance(raw, str) or not raw.strip():
        raise RequestError(400, "invalid", "'path' is required")
    if len(raw) > PROJECT_ADD_PATH_MAX:
        raise RequestError(413, "too_large", f"'path' is over {PROJECT_ADD_PATH_MAX} characters")
    if "\x00" in raw:
        raise RequestError(400, "invalid", NUL_IN_A_PATH.format(field="path"))
    try:
        path = Path(raw.strip()).expanduser()
    except RuntimeError:  # ~user, for a user this machine does not have
        raise RequestError(400, "invalid", f"{raw.strip()} does not exist") from None
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


def check_project_ref_on_disk(ref: str) -> None:
    """Refuse the ``project/remove`` ref the system will not look up as a path, before
    ``project_service.forget`` asks the disk about it: it tries a ref as a path first.

    A NUL byte is 400 ``invalid``. A name longer than any file's, or a ``~user`` with
    no home here, is 404 ``not_found``: no project has it for a root or a name. Left to
    ``forget``, the NUL's ``ValueError`` read as two projects matching (400
    ``ambiguous_project``), and the others fell to 400 ``write_failed``, each with the
    system's own words (``lstat: embedded null character in path``) for a write that
    never began (sweep 3 of #243).
    """
    if "\x00" in ref:
        raise RequestError(400, "invalid", NUL_IN_A_PATH.format(field="ref"))
    try:
        Path(ref).expanduser().exists()
    except (OSError, RuntimeError):
        raise RequestError(404, "not_found", f"no project matches {ref!r}") from None


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
    board_frame: ProjectSource | None = None
    """What a ``board`` frame carries, read for that alone (:func:`remote_board_payload`'s
    ``boards``); ``None``: :func:`remote_board_frame` of ``board``, as for a test's fakes."""


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


PANE_OUTLIVED = (
    "{label}'s pane is gone: tmux restarted after {label} started, "
    "and its pane id is another agent's now"
)
"""409 ``not_agent`` for a row that outlived its tmux server (:func:`_remote_facts_refusal`)."""


def _remote_live_row(target: ProjectInfo, label: str) -> FleetAgent:
    """The newest live row holding ``label`` in ``target``; :class:`NoSuchAgent` when none does."""
    from aisquare.core.store import store_session

    with store_session() as store:
        agent = store.fleet_agent_by_label(target.id, label, live_only=True)
    if agent is None:
        raise NoSuchAgent(f"no live agent {label!r} in {target.root.name or target.id}")
    return agent


PANE_NOT_AGENT = "{label}'s pane is not running the agent"
"""409 ``not_agent`` for a row whose pane runs something else (:func:`_remote_facts_refusal`)."""


def _remote_pane_facts(server: TmuxServer, agent: FleetAgent) -> PaneFacts | None:
    """The facts of the pane under the row's id, one ``display-message``; ``None`` when it is
    gone, and when tmux cannot be asked: that is not a pane to type into either."""
    from aisquare.core.tmux import TmuxError

    try:
        return server.pane_facts(agent.pane_id)
    except TmuxError:
        return None


def _remote_pane_refusal(server: TmuxServer, agent: FleetAgent) -> str | None:
    """Why nothing may be typed into the row's pane, a sentence about ``{label}``; ``None``
    when the pane is the agent's own: :func:`_remote_facts_refusal` on its facts."""
    return _remote_facts_refusal(agent, _remote_pane_facts(server, agent))


def _remote_facts_refusal(agent: FleetAgent, facts: PaneFacts | None) -> str | None:
    """Why nothing may be typed into the pane these facts are of, as :func:`_remote_pane_refusal`
    says it; ``None`` when it is the row's agent.

    It must run the agent (``fleet._runs_the_agent``), on the server the row was recorded
    on, asked in that order. A reboot or a hand-run ``tmux -L asq kill-server`` leaves live
    rows behind (the listing reads them ``lost`` and ends none of them), and the next
    server, started by a spawn in any project, numbers its panes from ``%0`` again: asked
    about by id, that agent's pane answered for the row, the phone showed its screen under
    the row's label, and a key from the pad answered its prompt. A server that started
    after the row was written holds none of its panes (``fleet._outlived``, FLEET-1), and a
    start tmux will not give judges nothing, as in ``fleet._pane_alive``.

    ``send-keys`` and needs-you's snapshot, which the quick answers and the agent actions
    type on the strength of, both judge here: the stale-pane rule in one place, where a
    second copy would miss the next restart signal it learns. Both judge one answer, which
    says when its server started (``PaneFacts.server_started``): the start was a second
    ``display-message``, so every key tapped cost two tmux processes, and every poll of an
    action three with needs-you's own for quietness (review of #243, round 4).
    """
    from aisquare.services import fleet as fleet_service

    if facts is None or not fleet_service._runs_the_agent(facts):
        return PANE_NOT_AGENT
    if fleet_service._outlived(agent, facts.server_started):
        return PANE_OUTLIVED
    return None


def _remote_keys_row(target: ProjectInfo, label: str, pin: str | None) -> FleetAgent:
    """The live row holding ``label`` (:func:`_remote_live_row`), which keys pinned to
    ``pin`` (``send-keys``' ``agent_id``) go to only when it is that row: 409 ``stale``
    otherwise, ``current`` naming the row that holds the label now, or none.

    A pinned key that finds no live row is ``stale`` as well, as a pinned action is
    (``remote_actions.action_gone``): in the gap of a restart's hand-over, after the old
    row ended and before the new one was made, it was 404 ``no_such_agent``, and the page
    left the agent's screen for the fleet it was about to come back to (merge of round 5
    of #243). Unpinned, it is still ``no_such_agent``.
    """
    try:
        agent = _remote_live_row(target, label)
    except NoSuchAgent:
        if pin is None:
            raise
        said = f"there is no agent {label!r} in {target.root.name or target.id} now"
        raise RequestError(
            409, "stale", f"{said} — nothing was sent", current={"agent_id": None}
        ) from None
    if pin is not None and agent.id != pin:
        raise RequestError(
            409,
            "stale",
            f"{label!r} is another agent now ({agent.id}) — nothing was sent",
            current={"agent_id": agent.id},
        )
    return agent


@contextlib.contextmanager
def _remote_keys_turn(
    target: ProjectInfo, label: str, pin: str | None = None
) -> Iterator[FleetAgent]:
    """Hold the agent's action lock while keys go to its pane; the row, read under it and
    judged against ``pin`` there (:func:`_remote_keys_row`).

    :func:`remote_agent_lock` is the one lock for every action on one agent, and keys
    typed while an action is half done land in the middle of it: in an Interrupt &
    tell between its Escape and its paste, which then submits them and the tell as one
    message, or between a stop's ``/exit`` and its Enter. So keys wait for their turn,
    up to :data:`SEND_KEYS_LOCK_WAIT_SECONDS`, and are 409 ``busy`` after that, as a
    second action is. A label no row holds makes no lock: the registry is process-wide
    and never shrinks, and a label is whatever a body says.

    The wait counts from when the request reached the server (:data:`_WRITE_ARRIVED`).
    Counted from when a thread was free to run it, keys tapped in a burst during an
    action waited their 2 s each in turn, a pool's worth at a time, and the last ones
    took the lock when the action let it go, seconds after their taps: typed into the
    replacement a restart had started (sweep of #243). Keys whose wait ran out before a
    thread was free to run them are 409 ``busy`` at a free lock as well: behind actions
    that held every thread of the write pool (a restart holds one for 20 to 40 s), a key
    ran as one of them ended, seconds after its tap, and typed into whatever its agent
    showed by then, the replacement a restart had started when that action was on it
    (review of #243, round 3).
    """
    _remote_keys_row(target, label, pin)
    lock = remote_agent_lock(target.id, label)
    arrived = _WRITE_ARRIVED.get()
    wait = SEND_KEYS_LOCK_WAIT_SECONDS
    if arrived is not None:
        wait -= time.monotonic() - arrived
    if wait <= 0:
        busy = f"the machine was busy with other actions for {SEND_KEYS_LOCK_WAIT_SECONDS:g} s"
        raise RequestError(409, "busy", f"{busy} — nothing was sent to {label}")
    if not lock.acquire(timeout=wait):
        raise RequestError(
            409, "busy", f"another action on {label} is still running — nothing was sent"
        )
    try:
        yield _remote_keys_row(target, label, pin)
    finally:
        lock.release()


def _live_panes(label: str, project: str | None = None, history: int = 0) -> dict[str, object]:
    """One pane frame: the live screen, or scrollback and the screen together (§4-L).

    ``history`` of 0 takes the same call the live stream always took and returns
    :func:`_pane_payload`'s keys and the row's ``agent_id``, with no history keys:
    those appear only when history was asked for.

    Every frame names the row it was captured from (``agent_id``), which the page
    sends with the keys typed at it (``send-keys``): a replacement that took the
    label since gets none of them. It also makes the replacement's first frame a
    change the stream sends, however like the last one its screen is.

    Never another agent's screen: a row whose pane id the next tmux server gave
    away is 409 ``not_agent`` (``fleet._outlived``, FLEET-1). The server says
    when it started in the very command that took the frame
    (``PaneFacts.server_started``), so the frame is judged by the server it came
    from. Asked in a second process after the capture, it doubled the stream's
    tmux processes: one more per watched pane per tick.
    """
    from aisquare.services import fleet as fleet_service

    agent = _remote_live_row(_resolve_project(project), label)
    server = fleet_service.server_for(agent.tmux_socket)
    if history <= 0:
        capture = server.capture(agent.pane_id)
    else:
        capture = server.capture_history(agent.pane_id, history=min(history, HISTORY_CAP))
    if fleet_service._outlived(agent, capture.facts.server_started):
        raise RequestError(409, "not_agent", PANE_OUTLIVED.format(label=label))
    payload = _pane_payload(capture)
    payload["agent_id"] = agent.id
    if history <= 0:
        return payload
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

    The cursor names its conversation, ``<session id>:<offset>``: an offset is a
    place in one file, and the label reads another after a ``/clear``, a fresh
    restart or a new agent under a freed label. A bare offset read the new file
    from there, and Load older put the new conversation above the old one as its
    past (review of #243, sweep of round 4). A cursor of another conversation, or
    of none this server made, is a 409 ``stale_cursor``.

    A page names the row it was read for (``agent_id``), as a pane frame does: the
    Transcript tab's Send carries it, so a reply to this conversation is typed into
    no replacement that took the label since (``send-keys``).
    """
    from aisquare.core.store import store_session
    from aisquare.services import transcript as transcript_service

    target = _resolve_project(project)
    with store_session() as store:
        agent = store.fleet_agent_by_label(target.id, label, live_only=True)
        if agent is None:
            raise NoSuchAgent(f"no live agent {label!r} in {target.root.name or target.id}")
        session = store.get_session(agent.session_id) if agent.session_id else None
    offset = None
    if before is not None:
        named, _, offset = before.rpartition(":")
        if session is None or named != session.id or not _remote_offset(offset):
            raise RequestError(
                409, "stale_cursor", f"{label} is in another conversation since that page"
            )
    path = session.transcript_path if session is not None else None
    page = transcript_service.read_page(
        path,
        limit=limit or transcript_service.DEFAULT_LIMIT,
        before=offset,
        width=width or _pane_width(agent),
    )
    payload = page.page_json()
    if page.cursor is not None and session is not None:
        payload["cursor"] = f"{session.id}:{page.cursor}"
    payload["agent_id"] = agent.id
    return payload


def _remote_offset(text: str) -> bool:
    """Whether ``text`` is a cursor's offset as the server writes it: a whole number past 0."""
    return text.isascii() and text.isdigit() and int(text) > 0


def _pane_width(agent: FleetAgent) -> int:
    """The agent's own pane width, so wrapped lines match the terminal they land in.

    Best effort by design: a dead pane, or a tmux that will not answer, costs a
    sensible 80 columns and never the page itself — the conversation is on disk
    and does not depend on the pane still being there. So does a pane id another
    agent's pane holds now (``fleet._outlived``, FLEET-1): its width is that agent's.
    One tmux process, the pane's facts alone, which say when their server started:
    a capture of the whole screen, then a second process to ask that, read a width.
    """
    from aisquare.services import fleet as fleet_service

    try:
        facts = fleet_service.server_for(agent.tmux_socket).pane_facts(agent.pane_id)
    except Exception:
        return 80
    if facts is None or fleet_service._outlived(agent, facts.server_started):
        return 80
    return facts.width


def remote_board_frame(board: object) -> dict[str, object]:
    """The ``board`` frame: what the page's Board tab draws of ``board_json`` (r3 #6).

    The events, and of the sessions only those an event names, by id, label and role.
    The rest is every session and task the project ever had, and the sessions change
    with every session's heartbeat (``last_seen_at``, ``cursor``): sent whole, the tab
    was sent all of it again several times a minute. ``GET api/board`` still answers
    the whole board, as ``asq board --json`` prints it.
    """
    whole = board if isinstance(board, dict) else {}
    events = whole.get("events")
    events = events if isinstance(events, list) else []
    named = {
        event["payload"].get("session_id")
        for event in events
        if isinstance(event, dict) and isinstance(event.get("payload"), dict)
    } - {None}
    sessions = whole.get("sessions")
    return {
        "project": whole.get("project"),
        "sessions": [
            {key: session.get(key) for key in ("id", "label", "role")}
            for session in (sessions if isinstance(sessions, list) else [])
            if isinstance(session, dict) and session.get("id") in named
        ],
        "events": events,
    }


def remote_board_unread(exc: Exception) -> dict[str, object]:
    """The ``board`` frame of a board that could not be read: the frame's keys, empty, and
    ``error``, the sentence that says why, as a ``pane`` frame says why it has no screen.

    The stream skipped such a frame and logged it at debug level, and the page's Board tab
    said "Loading…" for as long as it was open: under ``AISQUARE_TEAM=0``, for a project
    removed meanwhile, or while the store stayed locked (review of #243, round 4).
    """
    return {"project": None, "sessions": [], "events": [], "error": str(exc)}


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


BOARD_EVENTS = 200
"""The newest board events ``api/board`` and the ``board`` frame carry: as many as the page's
Board tab draws. ``asq board --json`` prints five, a glance in a terminal."""


BoardProjects = dict[tuple[Path | None, str], ProjectInfo]
"""The board project of each project root under each ``AISQUARE_TEAM_HUB``, resolved once."""


def remote_board_payload(
    project: str | None = None, *, boards: BoardProjects | None = None
) -> dict[str, object]:
    """``GET api/board`` and the ``board`` frame — the ONE call into ``board_data``.

    The project's root as ``cwd`` is exactly what ``asq board --json`` prints when
    run there, ``AISQUARE_TEAM_HUB`` included (``team_service._project``); ``None``
    is the current project, as it always was. With :data:`BOARD_EVENTS` events,
    not the CLI's five: the Board tab is the board, and with five a question a
    card sent the human to "reply on the board" to was gone from it once five
    newer lines were (review of #243, round 3).

    Under ``AISQUARE_TEAM_HUB`` every project's board is the hub's, and it names the
    hub's project, not the one asked for. So a ``board`` frame names the project its
    subscription named, and the page draws a read for the project it asked about. The
    page went by the board's own project id, dropped every frame and every read, and
    said "Loading…" for as long as the tab was open (review of #243, round 4).

    The frame passes ``boards``, and is read for what :func:`remote_board_frame` keeps
    of it. It is read every second while a phone shows its Board tab, and each read
    resolved the board through ``team_project``, a ``git rev-parse`` process, wrote the
    store (``ensure_project``, a write transaction beside every hook's), and read every
    session and task the project ever had, for the frame to drop all but a few (review
    of #243, round 4). Now the board project of each root is resolved once and kept in
    ``boards``, as the TUI's Board tab resolves its own once: neither the hub nor where
    a checkout's repository lives changes under a running server. ``board_data`` then
    reads the events and the sessions they name (``glance``). ``GET api/board`` still
    reads the whole board, as ``asq board --json`` prints it.

    #240 fold: pass ``exclude_kinds=team_service.CAPTAIN_AUDIT_KINDS`` here (one line).
    """
    from aisquare.cli.team import board_json
    from aisquare.core import orchestrator
    from aisquare.services import team as team_service

    cwd = None if project is None else _resolve_project(project).root
    board: ProjectInfo | None = None
    if boards is not None:
        key = (cwd, os.environ.get(orchestrator.TEAM_HUB_ENV_VAR, ""))
        board = boards.get(key)
        if board is None:
            board = boards[key] = team_service.resolve_project(cwd)
    return board_json(
        *team_service.board_data(cwd, events=BOARD_EVENTS, project=board, glance=boards is not None)
    )


def live_sources() -> Sources:
    """The real thing: the ``--json`` builders over the live store and tmux."""

    def projects_payload() -> object:
        from aisquare.cli.common import projects_json
        from aisquare.core.store import store_session
        from aisquare.services import fleet as fleet_service
        from aisquare.services import project as project_service

        all_projects = project_service.list_projects()
        # Listed: a project with a live row, or one that ended within the day (its window may
        # linger, an ``exited`` row). Any other lists no agent, and ``list_agents`` read every
        # row and session it ever had to say so, for each project, on every read (sweep of
        # #243, round 4).
        since = fleet_service._now() - fleet_service.RECENTLY_ENDED
        with store_session() as store:
            group_names = {g.id: g.name for g in store.project_groups()}
            listed = {
                one.id
                for one in all_projects
                if store.fleet_agents(one.id, live_only=True)
                or store.fleet_agents_ended_since(one.id, since)
            }
        rows = projects_json(all_projects, group_names=group_names)
        for row, one in zip(rows, all_projects, strict=True):
            agents = fleet_service.list_agents(one, live_only=True) if one.id in listed else []
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

    boards: BoardProjects = {}

    def board_frame_payload(project: str | None = None) -> object:
        return remote_board_frame(remote_board_payload(project, boards=boards))

    return Sources(
        projects=projects_payload,
        fleet=fleet_payload,
        board=remote_board_payload,
        tasks=tasks_payload,
        memory=memory_payload,
        panes=_live_panes,
        transcript=_live_transcript,
        explainability=_live_explainability,
        board_frame=board_frame_payload,
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
    fleet knows on a machine where the SDK is missing. It names no cost: the CLI
    carries no price table, and a guessed figure on a demo card is worse than none.
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
    payload["updated_at"] = _iso_seconds(max(stamps))
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


def _required(body: Mapping[str, Any], key: str) -> str:
    """A string the write cannot go without; 400 ``invalid`` when it is missing or blank.

    This and the readers below are the ONE way a write reads its body's fields, the
    agent actions' and the quick answers' included (``remote_actions.action_required``
    is this function): two copies drifted, one refusing a value of the wrong type and
    the other reading it as absent.
    """
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


def _optional_ref(
    body: Mapping[str, Any], key: str, *, limit: int | None = None, guard: bool = False
) -> str | None:
    """An optional NAME or reference — absent, null, blank and whitespace-only mean none.

    Any other value that is not a string is a 400 ``invalid``, never none: read as
    none, ``"project": 2048`` sent a write to the CURRENT project, keys typed into
    its agent included. Over ``limit`` is a 413. A ``guard`` (an id that keeps an
    action off the wrong agent: ``agent_id``, ``needs_id``) may not be blank either:
    read as none, the blank one a page sent from a card that had none would turn
    the guard off, and the action would still go through.

    Correct for a project ref, a note's task or a role. WRONG for literal text a
    human typed: see :func:`_literal`.
    """
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RequestError(400, "invalid", f"{key!r} must be a string")
    if limit is not None and len(value) > limit:
        raise RequestError(413, "too_large", f"{key!r} is over {limit} characters")
    if guard and not value.strip():
        raise RequestError(400, "invalid", f"{key!r} is blank: send the id, or leave it out")
    return value.strip() or None


def _literal(body: Mapping[str, Any], key: str) -> str | None:
    """Text to deliver verbatim — whitespace is CONTENT here, not emptiness.

    ``_optional_ref`` answers "did they name something", and a name that is all
    spaces is no name. A keystroke that is all spaces is a keystroke. Reading
    typed text with ``_optional_ref`` is what silently ate the space bar: a flush of
    ``" "`` became ``None``, so a write carrying only a space delivered nothing
    while the endpoint answered 200 ``sent: true``, and the audit line recorded
    ``text=0ch`` — the trail honestly reporting that no text was sent, the loss
    having happened before it. Absent or null is still absent; ``""`` is still
    nothing to send. A value that is not a string is a 400 ``invalid``: read as
    absent, ``"text": 3`` with ``"enter": true`` sent the Enter alone, which takes a
    dialog's highlighted option, and answered 200 ``sent: true``.
    """
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RequestError(400, "invalid", f"{key!r} must be a string")
    return value


def _remote_flag(body: Mapping[str, Any], key: str) -> bool:
    """An optional ``true`` or ``false``, and nothing else: absent or null is false, any
    other value a 400 ``invalid``.

    ``bool()`` of a JSON string is true for ``"false"``, ``"0"`` and ``"no"``: ``"enter":
    "false"`` pressed Enter after the keys it came with, which takes a dialog's
    highlighted option, and ``"force": "false"`` would kill an agent without its
    ``/exit``. ``send-keys``, the quick answers and the agent actions all read their
    flags here (``remote_actions.action_flag`` is this function).
    """
    value = body.get(key)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise RequestError(400, "invalid", f"{key!r} must be true or false")
    return value


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


@contextlib.contextmanager
def _remote_board_refusals(*refs: tuple[str, str | None, str]) -> Iterator[None]:
    """The board's refusals as ``asq`` gives them, as a write's: 409 ``team_disabled`` with
    the orchestrator off (``AISQUARE_TEAM=0``), as the agent actions answer it too, 409
    ``claim_lost`` for a task another session holds, and 400 ``ambiguous_id`` for a ref
    that names two tasks or sessions. They fell to 400 ``write_failed``, "the write
    failed", where nothing had, and to 404 ``not_found`` where two were found (sweep 2 of
    #243).

    A ref that names nothing is 404 ``not_found`` in a sentence that says which field
    sent it, from ``refs``, each ``(field, value, "task" | "session")``. The board's
    services raise a bare ``KeyError(ref)``, and its message was the ref in quotes,
    ``"'ses_nope'"``: a note whose ``as`` and ``task`` were the same could not say which
    one named nothing (sweep 3 of #243).
    """
    from aisquare.core.store import AmbiguousIdError
    from aisquare.services import team as team_service

    try:
        yield
    except team_service.TeamDisabledError as exc:
        raise RequestError(409, "team_disabled", str(exc)) from None
    except team_service.ClaimLostError as exc:
        raise RequestError(409, "claim_lost", str(exc)) from None
    except AmbiguousIdError as exc:
        said = f"{exc.ref!r} is ambiguous — use more characters"
        raise RequestError(400, "ambiguous_id", said) from None
    except KeyError as exc:
        missing = exc.args[0] if len(exc.args) == 1 else None
        named = [(field, kind) for field, value, kind in refs if value == missing]
        if not isinstance(missing, str) or not named:
            raise
        field, kind = _remote_ref_unknown(missing, named)
        said = f"no {kind} matches {missing!r} (the {field!r} field)"
        raise RequestError(404, "not_found", said) from None


def _remote_ref_unknown(ref: str, named: list[tuple[str, str]]) -> tuple[str, str]:
    """Which of ``named``, the ``(field, kind)`` of every field that sent ``ref``, names
    nothing: the one field, or when two sent the same ref (``as`` and ``task``), the
    first one the store has nothing for. The services raise ``KeyError(ref)`` for each."""
    if len(named) > 1:
        from aisquare.core.store import store_session

        with contextlib.suppress(Exception), store_session() as store:
            for field, kind in named:
                found = store.get_session(ref) if kind == "session" else store.get_task(ref)
                if found is None:
                    return field, kind
    return named[0]


def live_writes() -> Writes:
    """The write endpoints over the services the CLI commands call, then the agent actions."""
    from aisquare.services import remote_actions

    exit_keys = _ExitKeyGuard()

    def task_claim(body: dict[str, Any]) -> tuple[dict[str, object], str]:
        from aisquare.services import team as team_service

        ref, author = _required(body, "ref"), _optional_ref(body, "as")
        with _remote_board_refusals(("ref", ref, "task"), ("as", author, "session")):
            task = team_service.claim_task(ref, session_ref=author)
        return {"task": task.model_dump(mode="json")}, f"claimed {task.id} as={author or '-'}"

    def task_done(body: dict[str, Any]) -> tuple[dict[str, object], str]:
        """Close a task, with a ``note`` held to a note's rules (:func:`check_note_text`)."""
        from aisquare.services import team as team_service

        ref, author = _required(body, "ref"), _optional_ref(body, "as")
        note = _optional_ref(body, "note")
        if note is not None:
            check_note_text(note, "note")
        with _remote_board_refusals(("ref", ref, "task"), ("as", author, "session")):
            task = team_service.finish_task(ref, note=note, session_ref=author)
        return {"task": task.model_dump(mode="json")}, f"done {task.id} as={author or '-'}"

    def write_note(body: dict[str, Any]) -> tuple[dict[str, object], str]:
        """A note on a project's board: ``project``'s, or the current one's without it.

        The board resolves from the project's root exactly as ``asq note`` run
        there would; with ``as``, the session's own board still wins (the CLI's
        rule), so a note posted as an agent lands where that agent reads. Only
        the human's kinds (:data:`NOTE_KINDS`): ``kind`` went to the board and the
        audit line as it came, so a phone could forge the fleet's own reports and,
        with a newline in it, a line of the audit trail. The summary records who
        the note claims to be from (``as=``) and who it is for (``to=``), in that
        order, and ``to`` quoted: it is whatever the body says, and written before
        ``as=`` and bare, ``"to": "coder-1 as=manager"`` read as a note posted as
        the manager, and 300 characters of it cut the real ``as=`` off the line
        (sweep of #243). ``to`` holds only characters that print
        (:func:`check_note_to`). ``as`` must name a session, or the note is refused,
        and so is a ``task`` of another project's board: 400 ``invalid``, where it fell to
        ``write_failed``, as if the write had failed (sweep 3 of #243). Said for the
        phone, by its field: the board's sentence names ``asq note``'s ``--task``. Only
        that refusal: another ``ValueError`` from deeper in (a pydantic one, a text that
        will not encode) is still a write that failed.
        """
        from aisquare.services import team as team_service

        project = _optional_ref(body, "project")
        text = _required(body, "text")
        check_note_text(text, "text")
        kind = _optional_ref(body, "kind") or "note"
        if kind not in NOTE_KINDS:
            kinds = ", ".join(sorted(NOTE_KINDS))
            raise RequestError(400, "invalid", f"'kind' must be one of {kinds}")
        author, to = _optional_ref(body, "as"), _optional_ref(body, "to", limit=NOTE_TO_MAX)
        if to is not None:
            check_note_to(to)
        task = _optional_ref(body, "task")
        try:
            with _remote_board_refusals(("as", author, "session"), ("task", task, "task")):
                event = team_service.add_note(
                    text,
                    session_ref=author,
                    task_ref=task,
                    to_role=to,
                    kind=kind,
                    cwd=None if project is None else _resolve_project(project).root,
                )
        except ValueError as exc:  # a task of another project's board, as ``asq note`` says
            if task is None or type(exc) is not ValueError:
                raise  # not the board's refusal: a write that failed
            said = f"{task!r} is a task of another project's board (the 'task' field)"
            raise RequestError(400, "invalid", said) from None
        addressed = "-" if to is None else json.dumps(to)
        summary = f"{event.kind} seq={event.seq} as={author or '-'} to={addressed}"
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
        """Add a project the phone names, within :func:`check_project_add_root`'s limits;
        ``added`` is false when it was listed already.

        An add on purpose (``onboard_project``), as ``project switch`` and ``link``
        are, never the hooks' capture (``ensure_project``): a capture is never listed,
        so the phone was told ``added`` and its Projects screen, ``project list`` and
        the sidebar did not change. A directory the hooks captured, or one forgotten,
        is listed from now on, so it is added.
        """
        from aisquare.core.store import store_session
        from aisquare.core.workspace import project_id_for
        from aisquare.models import ProjectInfo

        root = check_project_add_root(body.get("path"))
        project = ProjectInfo(id=project_id_for(root), root=root, linked_repos=[])
        with store_session() as store:
            known = store.get_project(project.id)
            project = store.onboard_project(project)
        added = known is None or known.onboarded_at is None
        payload = {"project": project.model_dump(mode="json"), "added": added}
        return payload, f"added {project.id} {root}"

    def project_remove(body: dict[str, Any]) -> tuple[dict[str, object], str]:
        """Forget a registration, as ``project forget`` does, refusals and their codes
        included: one with live fleet agents is 409 ``project_busy``, where it fell to
        400 ``write_failed`` as if the write had failed (sweep 2 of #243), and a ref the
        system will not look up is refused first (:func:`check_project_ref_on_disk`)."""
        from aisquare.services import project as project_service

        ref = _required(body, "ref")
        check_project_ref_on_disk(ref)
        try:
            report = project_service.forget(ref, purge=False)
        except KeyError:
            raise RequestError(404, "not_found", f"no project matches {ref!r}") from None
        except ValueError as exc:
            raise RequestError(400, "ambiguous_project", str(exc)) from None
        except project_service.ProjectBusyError as exc:
            raise RequestError(409, "project_busy", str(exc)) from None
        return {"report": _as_json(report)}, f"removed {ref}"

    def write_send_keys(body: dict[str, Any]) -> tuple[dict[str, object], str]:
        """Type into one agent's pane: ``text`` (as hex, nothing parses it), or pad ``keys``.

        Everything is checked before anything is sent: each field's type (a
        ``"enter": "false"`` is a 400, not an Enter), the keys against the
        allowlist, the caps, no control character in the text, one input per body
        (``text`` went first, so "Esc, then type" arrived as "type, then Esc"), the
        pane, and the double Ctrl-C. The pane must be running the agent, and be the
        row's own: after a tmux restart the row's pane id names another agent's pane
        (409 ``not_agent``, as the actions and quick answers refuse it). The pane is
        judged and typed into under the agent's action lock (:func:`_remote_keys_turn`).
        Once a byte may have reached the pane, a failure is still audited: the trail
        exists for what a device did to a live agent, finished or not.

        ``dialog_guard`` is for a sender that does not see the pane, the page's
        Transcript tab: nothing is typed while the agent may be showing a dialog,
        which the text and its Enter would answer (409 ``dialog_open``,
        ``remote_actions.action_keys_guard``). The Live tab shows the dialog, and
        its keys are how one is answered, so they go without it.

        ``agent_id`` pins the keys to the row whose screen they were typed at, which a
        pane frame and a transcript page name: once another row holds the label, or none
        does, nothing is sent (409 ``stale``, ``current`` naming that row or none,
        :func:`_remote_keys_row`), as for every other write that types into an agent.
        Unpinned, a key tapped at the prompt the
        phone showed went into the replacement a ``fleet restart`` or a usage-limit
        hand-over had started since, neither of which takes the agent's lock: into its
        input box, ahead of the line the fleet types into a resumed agent, or into its
        first dialog (review of #243, round 5).
        """
        from aisquare.services import fleet as fleet_service

        label = _required(body, "agent")
        text = _literal(body, "text")
        keys = [] if body.get("keys") is None else check_remote_key_names(body["keys"])
        enter = _remote_flag(body, "enter")
        confirmed = _remote_flag(body, "confirm_exit")
        guarded = _remote_flag(body, "dialog_guard")
        pin = _optional_ref(body, "agent_id", guard=True)
        project = _optional_ref(body, "project")
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
        target = _resolve_project(project)
        summary = (
            f"{label}@{target.id} text={len(text or '')}ch keys={_audit_keys(keys)} enter={enter}"
        )
        with _remote_keys_turn(target, label, pin) as agent:
            server = fleet_service.server_for(agent.tmux_socket)
            refusal = _remote_pane_refusal(server, agent)
            if refusal is not None:
                said = refusal.format(label=label)
                raise RequestError(409, "not_agent", f"{said} — nothing was sent")
            if guarded:
                remote_actions.action_keys_guard(target, label, agent.id)
            exits = sum(key in EXIT_KEYS for key in keys)
            if exits and not exit_keys.exit_keys_allowed(
                (target.id, label), exits, confirmed=confirmed
            ):
                raise RequestError(409, "double_press", DOUBLE_PRESS)
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
    """At most ``limit`` calls per ``window`` seconds per key, then a wait: the one sliding
    window every throttle of the server counts with, one instance per rule.

    Unlock attempts per client (:data:`UNLOCK_LIMIT` per :data:`UNLOCK_WINDOW_SECONDS`),
    the client being uvicorn's resolved peer (:func:`_client_of`), never a header the
    sender writes; and the push routes' calls per device (``remote_push.push_routes``),
    which kept a copy of this that a fix to one would have missed (review of #243,
    round 4). A key whose window has emptied is forgotten on the next call by anyone,
    so the table holds the keys of the last window and no more: keyed on a header, a
    fresh invented address per request grew it forever. Called on the event loop alone,
    with no ``await`` between a route's check and its count, so it takes no lock.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._attempts: dict[str, deque[float]] = {}

    def limiter_retry_after(self, key: str, limit: int, window: float) -> float | None:
        """Count a call by ``key``: ``None`` when it may go ahead, else the seconds until
        it may (and nothing is counted)."""
        now = self._clock()
        for known, held in list(self._attempts.items()):
            while held and now - held[0] >= window:
                held.popleft()
            if not held:
                del self._attempts[known]
        held = self._attempts.setdefault(key, deque())
        if len(held) >= limit:
            return window - (now - held[0])
        held.append(now)
        return None

    def limiter_wait_seconds(self, key: str, limit: int, window: float) -> int | None:
        """:meth:`limiter_retry_after` as a ``Retry-After`` says it: whole seconds, rounded up,
        never 0."""
        retry = self.limiter_retry_after(key, limit, window)
        return None if retry is None else max(1, math.ceil(retry))


@dataclass(frozen=True, eq=False)
class _Failed:
    """A snapshot that raised (:class:`_Cache`): the exception and the traceback it was
    raised with. It is the tick's answer as a value is, and each caller it is raised to
    gets that traceback back, so the traceback does not grow by every caller's frames."""

    error: BaseException
    traceback: TracebackType | None


def _cache_answer(outcome: object) -> object:
    """What a caller of :class:`_Cache` takes from an outcome: the snapshot, or its failure,
    raised."""
    if isinstance(outcome, _Failed):
        raise outcome.error.with_traceback(outcome.traceback)
    return outcome


class _Cache:
    """One snapshot per kind per tick, however many sockets are open: each cached read,
    and each pane the stream's sockets watch (one capture, not one per socket).

    It holds only what was asked for within the last tick. A kind carries the
    ``?project=`` ref as it was written, and every spelling that resolves is a
    kind of its own (an id prefix of any length, a name, a codename, and the id
    with any run of ``*``, ``?`` or ``[``, which the store's glob drops). Kept
    until they were asked for again, they grew the heap by a full payload per
    spelling for anyone unlocked, read-only included, until the process died.
    So each store first drops what has expired, and at most
    :data:`CACHE_KINDS_MAX` kinds are kept, the oldest going first.

    One caller at a time computes a kind, and only that kind's callers wait for
    it. The snapshots were computed under the one lock that guards the table,
    so the slowest held up every other: ``projects`` lists every project's
    fleet, a tmux call each, and a tmux that stops answering costs 30 s a call,
    while every socket's board and fleet frames, and every cached read, waited
    behind it.

    What a compute comes to is the tick's answer, a failure as much as a value,
    and every caller of the tick takes that. Only a value was kept, so the
    callers waiting on a kind that raised computed it again one after another:
    a store locked past its 5 s ``busy_timeout`` held the Nth socket's ``board``
    frame N x 5 s, its ``fleet`` frame as long again, and its heartbeat behind
    both.

    A caller waits on the event loop, and only the one computing takes a
    thread. Each caller waited in a thread of the loop's default pool, which
    also runs every unlock, write and transcript read, so a few sockets waiting
    on one slow kind held all of it.

    An outcome is as old as its read: its age counts from when its compute was
    claimed, not from when it came back. Counted from its end, a socket's own
    read was still within the ttl at its next tick, a tick after it began,
    whenever it took more than a tenth of one: the socket took its own last
    snapshot back, the frame was the same, none went out, and a board, a fleet or
    a pane that slow came every two ticks (sweep of #243, round 5). Sockets out of
    step still share one read within the tick, and an outcome whose read took the
    whole ttl answers the callers that waited on it and is kept for none after.
    """

    def __init__(self, ttl: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        """Guards the two tables, and is never held while a snapshot is computed."""
        self._values: dict[str, tuple[float, object]] = {}
        """Each kind's outcome within the tick, with when its compute was claimed: its
        snapshot, or :class:`_Failed`."""
        self._flights: dict[str, tuple[Future[object], float]] = {}
        """The kinds being computed now, each to the future its callers wait on and when it
        was claimed. A flight goes when its compute ends, so this holds the kinds in flight
        and no more, whatever kinds are asked for."""

    def _cache_fresh(self, kind: str) -> tuple[float, object] | None:
        """``kind``'s outcome while it is younger than the ttl; call it holding ``_lock``."""
        hit = self._values.get(kind)
        return hit if hit is not None and self._clock() - hit[0] < self._ttl else None

    def _cache_store(self, kind: str, outcome: object, began: float) -> None:
        """Keep ``outcome`` as ``kind``'s, as old as its compute (``began``), and drop what
        expired, the outcome itself if its compute took the whole ttl; hold ``_lock``."""
        self._values.pop(kind, None)  # stored anew, so the dict stays in the order stored
        self._values[kind] = (began, outcome)
        now = self._clock()
        for stale in [k for k, (at, _kept) in self._values.items() if now - at >= self._ttl]:
            del self._values[stale]
        while len(self._values) > CACHE_KINDS_MAX:
            del self._values[next(iter(self._values))]

    def _cache_claim(self, kind: str) -> tuple[Future[object], bool]:
        """``kind``'s outcome as a future, and whether this caller is the one to compute it:
        settled already for what the tick kept, the flight another caller computes, or a
        new flight (``True``), which :meth:`_cache_compute` settles."""
        from concurrent.futures import Future

        with self._lock:
            hit = self._cache_fresh(kind)
            if hit is None:
                flying = self._flights.get(kind)
                if flying is not None:
                    return flying[0], False
                flight: Future[object] = Future()
                self._flights[kind] = (flight, self._clock())
                # Running, so a waiter that is cancelled (its socket closed) ends its own
                # wait and never the flight the other callers wait on.
                flight.set_running_or_notify_cancel()
                return flight, True
        kept: Future[object] = Future()
        kept.set_result(hit[1])
        return kept, False

    def _cache_compute(self, kind: str, flight: Future[object], compute: Snapshot) -> None:
        """Compute ``kind`` in a worker thread and settle its flight with what came of it. An
        exception is kept for the tick as a value is; anything rarer (``SystemExit``) only
        reaches the callers waiting now."""
        try:
            outcome: object = compute()
        except BaseException as exc:
            failed = _Failed(exc, exc.__traceback__)
            self._cache_settle(kind, flight, failed, keep=isinstance(exc, Exception))
        else:
            self._cache_settle(kind, flight, outcome, keep=True)

    def _cache_settle(
        self, kind: str, flight: Future[object], outcome: object, *, keep: bool
    ) -> None:
        """End ``kind``'s flight with ``outcome``, kept as the tick's when ``keep``; a flight
        ends once, and a later ending changes nothing."""
        with self._lock:
            flying = self._flights.get(kind)
            if flying is None or flying[0] is not flight:
                return
            del self._flights[kind]
            if keep:
                self._cache_store(kind, outcome, flying[1])
        flight.set_result(outcome)

    def _cache_job_done(self, kind: str, flight: Future[object]) -> None:
        """A compute's job ended: if it never ran (its pool shut down first), its flight ends
        here, since its callers must not wait for it forever."""
        if not flight.done():
            never = RuntimeError("the snapshot was not taken: its thread pool shut down")
            self._cache_settle(kind, flight, _Failed(never, None), keep=False)

    async def cached_snapshot(
        self, kind: str, compute: Snapshot, pool: Executor | None = None
    ) -> object:
        """``kind``'s snapshot this tick: what the tick kept, what the compute in flight comes
        to, or this caller's own compute, run in a thread of ``pool`` (``None``: the loop's
        default pool). A failure is raised to every caller it is the answer for."""
        import asyncio
        import contextvars

        flight, mine = self._cache_claim(kind)
        if mine:
            run = functools.partial(
                contextvars.copy_context().run, self._cache_compute, kind, flight, compute
            )
            try:
                job = asyncio.get_running_loop().run_in_executor(pool, run)
            except BaseException as exc:  # a pool shut down: the callers waiting hear why
                self._cache_settle(kind, flight, _Failed(exc, exc.__traceback__), keep=False)
                raise
            job.add_done_callback(lambda _job: self._cache_job_done(kind, flight))
        if not flight.done():
            await asyncio.wrap_future(flight)
        return _cache_answer(flight.result())


_UNSENT = object()
"""No frame of the kind has gone out on the socket yet (``stream``)."""


_UNREAD = object()
"""A snapshot a socket's tick is still reading (``stream``)."""


def _read_outcome(read: asyncio.Future[object]) -> object:
    """What a socket's finished read came to: its snapshot, or the exception it raised.
    Anything rarer than an exception (``SystemExit``) is raised, as it was when the tick
    awaited the read itself."""
    failed = read.exception()
    if failed is not None and not isinstance(failed, Exception):
        raise failed
    return read.result() if failed is None else failed


def _let_read_go(read: asyncio.Future[object]) -> None:
    """A socket's read that nothing will take: its wait ends, and its compute runs on for
    whoever else waits on it (:class:`_Cache`). One that ended with an exception has it
    taken, so asyncio does not log it as never retrieved."""
    if not read.done():
        read.cancel()
    elif not read.cancelled():
        read.exception()


def _same_frame(sent: object, payload: object) -> bool:
    """Whether ``payload`` says what the frame that sent ``sent`` said: the very object, as
    the needs feed is for the three seconds between scans, or one equal to it.

    The stream encoded each payload as JSON with ``sort_keys``, per socket, per tick and on
    the event loop that serves every request, to compare the text with the last frame's:
    a feed of 20 items is 100 to 300 KB to encode, and each watched pane 20 KB more
    (review of #243, round 4). ``==`` builds no string: it takes a sub-object that is the
    very one as equal at once, so the feed costs nothing, and walks an equal snapshot in
    C. A payload is JSON built anew, never changed once made, whose values keep their
    types from one tick to the next, so equal payloads are equal frames.
    """
    return sent is payload or sent == payload


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


def _built_page_file(dist: Path, rel: str) -> Path | None:
    """The file of an installed or ``--dist`` build that ``rel`` names, or ``None``: one
    inside ``dist``, no part of it hidden, that the system will look up.

    A path the system refuses, a NUL byte (``ValueError``) or a name past its limit
    (``ENAMETOOLONG``), is a file this build does not have, and the request goes on to
    the 404 or the document as any other miss does: it raised, and the page answered a
    bare 500 with a traceback in the log for each, to anyone with the link (sweep 3 of
    #243).

    A hidden file is not the page's either, as the bundled page leaves its dotfiles out
    (:func:`remote_page.bundled_page_files`), judged as asked and where it resolves:
    every file below the directory was served without the passphrase, and a project's
    own directory installed or served in place of its ``dist/`` gave out its ``.env``
    and ``.git/config`` (sweep 3 of #243).
    """
    if any(part.startswith(".") for part in PurePosixPath(rel).parts):
        return None
    candidate = _built_page_target(dist, dist / rel)
    try:
        return candidate if candidate is not None and candidate.is_file() else None
    except (OSError, ValueError):
        return None


def _built_page_target(dist: Path, path: Path) -> Path | None:
    """Where ``path`` resolves when a build in ``dist`` may serve what is there: inside
    ``dist``, with no hidden part; else ``None``, a path the system refuses included.
    The one rule for what is served (:func:`_built_page_file`) and what ``install-page``
    copies (:func:`_page_copy_skips`)."""
    try:
        resolved = path.resolve()
    except (OSError, ValueError):
        return None
    if not resolved.is_relative_to(dist):
        return None
    if any(part.startswith(".") for part in resolved.relative_to(dist).parts):
        return None
    return resolved


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


LINK_GONE = (
    "Remote is off on the machine, or the link changed — turn it on again, or open the "
    "link the machine shows now"
)
"""404 ``not_found`` at the token gate, in the words of the page's screen for it and of
docs/remote.md's troubleshooting. A wrong token and a passed auto-off answer alike, and
with one sentence whatever was sent, so it tells a guess nothing about the token."""
NOT_UNLOCKED = "no unlocked device for this request — unlock with the passphrase"
"""401 ``unauthorized``: no cookie, or one whose device is signed out, revoked or expired."""
WRONG_PASSWORD = "that is not the passphrase"
"""401 ``wrong_password``."""
NOT_TEXT = "the body holds a lone surrogate (an unpaired \\ud800-\\udfff escape), which is not text"
"""400 ``invalid`` for a body string no UTF-8 can hold (:meth:`RemoteKit.kit_json_object`)."""
STATE_UNWRITABLE = (
    "the machine could not save that: its ~/.aisquare/remote.json would not write (a full disk, "
    "or a home it may not write) — nothing was changed; fix that on the machine, then try again"
)
"""503 ``remote_state_unwritable``, as ``asq remote``'s own commands say it, but without the
path or the error, which a phone that has not unlocked yet may read: the log has both."""
REVOKE_UNSAVED = (
    "revoked on the running Remote, but the machine's ~/.aisquare/remote.json would not write "
    "(a full disk, or a home it may not write): once it can, run  aisquare remote revoke {device}  "
    "on the machine, as until it is saved a change to that file from a shell, or a Remote "
    "turned on again, would take the device back"
)
"""503 ``remote_state_unwritable`` for a revoke, ``{device}`` its id: it holds in memory, where
the gate reads it, and the next flush that can write saves it (:meth:`Runtime.flush_last_seen`
writes memory whenever it differs from the file). Not always: a write from another process
before then is read by this one (``reload_if_changed``) with the device still in it, so the
sentence names the command that makes the revoke hold (review of #243, round 4)."""
CRASHED = "the machine hit an error answering that"
"""500 ``internal_error``: what the ledger answers a retry of a request that crashed, in the
words the page uses for a crash."""


def _error_body(error: str, message: str) -> dict[str, object]:
    """``{error, message}``, the one shape of every refusal (SPEC §0.5).

    The message is always there: the docs promise one, and the page shows it, so a
    refusal without one left the unlock line saying ``not_found``. A message that came
    out empty, the ``str()`` of an exception that holds no text, is the code in words.
    And a sentence that echoes what a request or the disk held, a name or a path, may
    hold a lone surrogate, which no response can encode: each is a ``?``, or the
    refusal would end as a bare 500 in plain text instead.
    """
    said = message or error.replace("_", " ")
    return {"error": error, "message": said.encode("utf-8", "replace").decode("utf-8")}


def _json_error(status: int, error: str, message: str) -> Response:
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


COOKIE_VALUES_MAX = 8
"""How many ``asq_remote`` values of one request are asked about: a browser sends one per
path and domain that set one, the device's own first (:func:`remote_cookie_values`)."""


def remote_cookie_values(scope: Any) -> list[str]:
    """Every ``asq_remote`` value the request's ``Cookie`` headers carry, in the order sent,
    at most :data:`COOKIE_VALUES_MAX`.

    Starlette keeps the LAST value of a name sent twice, and a browser sends the one
    with the longer path first (RFC 6265 §5.4): the device's own, ``Path=/r/<token>``.
    So an ``asq_remote=x; Path=/`` set by any page of the same host on another port,
    or by a sibling subdomain, was the one read, and the phone was a stranger from then
    on: 401 at every request, its unlocks counted against every phone's budget, a new
    device at each (sweep 3 of #243). Every value is read instead, and the first that
    names a device is the device.
    """
    values: list[str] = []
    for name, raw in scope.get("headers") or []:
        if name != b"cookie":
            continue
        for chunk in raw.decode("latin-1").split(";"):
            key, _sep, value = chunk.partition("=")
            if key.strip() == COOKIE and value.strip():
                values.append(value.strip())
    return values[:COOKIE_VALUES_MAX]


def remote_gate_device(runtime: Runtime, scope: Any) -> Device | None:
    """Gate 4: the unlocked device behind the request's ``asq_remote`` cookie, or ``None``:
    the first of its values that names one (:func:`remote_cookie_values`)."""
    for secret in remote_cookie_values(scope):
        device = runtime.device_for_cookie(secret)
        if device is not None:
            return device
    return None


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
    message: str,
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
        # remote.json is checked for another process's change once, here, for the gates
        # and, over HTTP, for the route behind them (Runtime.remote_state_checked).
        with self._runtime.remote_state_checked():
            passed = await self._remote_gated(scope, receive, send)
            if passed is not None and kind == "http":
                await self._app(scope, passed, send)
                return
        if passed is not None:
            # A socket lives for hours: its stream checks the file once a tick instead.
            await self._app(scope, passed, send)

    async def _remote_gated(self, scope: Any, receive: Any, send: Any) -> Any | None:
        """The five gates: the ``receive`` to hand the app, or ``None`` once a refusal went."""
        runtime = self._runtime
        kind = scope.get("type")
        if not (remote_gate_token(runtime, scope) and remote_gate_auto_off(runtime, scope)):
            await _refuse_at_the_gate(
                scope, receive, send, 404, "not_found", LINK_GONE, WS_CLOSE_NOT_FOUND
            )
            return None
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
            return None
        if _needs_a_device(scope):
            device = remote_gate_device(runtime, scope)
            if device is None:
                await _refuse_at_the_gate(
                    scope, receive, send, 401, "unauthorized", NOT_UNLOCKED, WS_CLOSE_UNAUTHORIZED
                )
                return None
            scope[DEVICE_SCOPE] = device
        if kind == "http" and method not in ("GET", "HEAD", "OPTIONS"):
            replayed = await remote_gate_body(scope, receive)
            if replayed is None:
                too_large = f"the body is over {MAX_BODY_BYTES} bytes"
                await _json_error(413, "too_large", too_large)(scope, receive, send)
                return None
            return replayed
        return receive


# --- the kit: what every route of one app shares ---------------------------------------


IN_PROGRESS = "a request with this request_id is still running — its answer will follow"
"""409 ``in_progress``: a retry that arrived while the first try was still running."""
REQUEST_ID_REUSED = (
    "request_id {request_id} was sent with another request — give each write a request_id of "
    "its own, and send one again only with the request it was first sent with"
)
"""409 ``request_id_reused``: an id the ledger holds, with another endpoint or body."""
ALREADY_ANSWERED = (
    "request_id {request_id} was answered {status} already, and that answer is no longer "
    "kept — it is not run again; send it with a new request_id to run it again"
)
"""409 ``already_answered``: a retry of a request that ran, whose answer went to make room for
the device's newer ones (``remote_actions.ACTION_LEDGER_SIZE``)."""


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


def _ledger_request(endpoint: str, body: dict[str, Any]) -> str:
    """What a ``request_id`` stands for: the endpoint and the body, its id taken out, as one
    digest. A retry sends both again as they were (the page resends the very body); a
    write that reuses the id for anything else is another request (:meth:`RemoteKit.kit_gated`).
    """
    try:
        canonical = json.dumps([endpoint, body], sort_keys=True, separators=(",", ":"))
    except (ValueError, RecursionError):  # nested past what json recurses into
        raise RequestError(400, "invalid", "the body must be a JSON object") from None
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


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
    write_pool: ThreadPoolExecutor | None = None
    """Made on the first write (:meth:`kit_write_pool`); the lifespan shuts it down."""
    sockets: dict[str, list[Callable[[int], None]]] = field(default_factory=dict)
    """Each device's live sockets, oldest first, as closers that take a close code."""
    lane_state: dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _write_waiting: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    """Per device id, its writes waiting for a thread of the write pool (:meth:`kit_run_write`)."""

    def kit_device(self, request: HTTPConnection) -> Device:
        """The device gate 4 found for this request; the cookie is never looked up twice."""
        device = request.scope.get(DEVICE_SCOPE)
        if not isinstance(device, Device):
            # Only a route outside the gate's reach can get here: refuse, never guess.
            raise RequestError(401, "unauthorized", NOT_UNLOCKED)
        return device

    async def kit_json_object(self, request: Request) -> dict[str, Any]:
        """THE body parser: the JSON object the request carries, ``{}`` for none at all.

        No other code in a remote module reads a body (``tests/test_remote_gates.py``
        pins it), so every route refuses a malformed one the same way: 400 ``invalid``.
        That includes a body nested deeper than ``json`` recurses into: it raises
        ``RecursionError``, not ``ValueError`` (from about 1 000 levels on 3.11), and
        anyone holding only the URL can post one to ``unlock``. And a string holding a
        lone surrogate, which ``json`` reads from a ``\\ud800`` escape and no UTF-8
        can hold: a refusal that echoed one (a stop's ``confirm=<label>``, a path
        ``project/add`` would not take) could not be encoded, and answered a bare 500
        in plain text that the ledger kept as a crash (sweep of #243).
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
        try:
            json.dumps(body, ensure_ascii=False).encode("utf-8")
        except UnicodeEncodeError:
            raise RequestError(400, "invalid", NOT_TEXT) from None
        except (ValueError, RecursionError):
            raise RequestError(400, "invalid", "the body must be a JSON object") from None
        return body

    def kit_refuse(
        self,
        status: int,
        error: str,
        message: str,
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
        """One line in ``remote-audit.log`` for what a device did, best effort: every caller
        has done it already, so a log that will not write is told in the server's log
        and the answer stands.

        An unlock's line that raised answered a bare 500 with no cookie, the device on
        disk and signed in (sweep 2 of #243). Every write's did the same after the
        write had run, the ledger holding its 200: on a full disk the phone said a
        send-keys, an extend or a note had failed, and the human sent it again, typed
        twice (sweep 3 of #243). The summary in the server's log is cleaned as the
        audit line's would be: it holds what the body said.
        """
        try:
            self.runtime.audit(device.id, endpoint, summary)
        except OSError as exc:
            log.warning(
                "remote: a %s audit line could not be written (%s): %s",
                endpoint,
                exc,
                _audit_clean(summary, AUDIT_SUMMARY_MAX),
            )

    def kit_write_allowed(self) -> bool:
        """Whether writes are on right now (``remote.json``, re-read when it changes)."""
        return self.runtime.allow_write

    def kit_write_still_allowed(self, device: Device) -> None:
        """Refuse a write that waited while what let it in changed: 404 ``not_found`` once
        auto-off passed, 401 ``unauthorized`` once its device is gone or signed out, 403
        ``read_only`` once writes are off. Called in the thread that runs the write, right
        before it does.

        The gates read all three when the request arrived, and a write then waited for a
        thread of the write pool, behind restarts that hold one for 20 to 40 s: one sent
        just before ``allow-write off``, a revoke, auto-off or Remote off (which revokes
        every device) still ran once a thread came free, the audit naming a device revoked
        minutes before (sweep 2 of #243). So ``remote.json`` is read afresh here, whatever
        the request's own check (:meth:`Runtime.remote_state_checked`) found back then. A
        write that has started is left to finish.
        """
        runtime = self.runtime
        checked = _STATE_CHECKED.set(None)
        try:
            with runtime.remote_state_checked():
                if runtime.auto_off_passed(_remote_now()):
                    raise RequestError(404, "not_found", LINK_GONE)
                if not runtime.device_is_live(device.id):
                    raise RequestError(401, "unauthorized", NOT_UNLOCKED)
                if not runtime.allow_write:
                    raise RequestError(403, "read_only", READ_ONLY_REASON)
        finally:
            _STATE_CHECKED.reset(checked)

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

        Never the default thread pool, which serves every HTTP read and every
        socket's board and fleet snapshot, nor the write pool (:meth:`kit_write_pool`):
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

    def kit_write_pool(self) -> ThreadPoolExecutor:
        """The pool every write handler runs on, made on first use.

        Never the default thread pool either, which runs every read and every socket's
        board and fleet snapshot: a send-keys waits there for its agent's lock while an
        action holds it, and a burst of taps during a restart held all of that pool's
        threads, so the stream and every read stalled for seconds (sweep of #243).
        """
        with self._lock:
            if self.write_pool is None:
                from concurrent.futures import ThreadPoolExecutor

                self.write_pool = ThreadPoolExecutor(
                    max_workers=WRITE_WORKERS, thread_name_prefix="asq-remote-write"
                )
            return self.write_pool

    async def kit_run_write(
        self, handler: WriteHandler, body: dict[str, Any], arrived: float, device: Device
    ) -> tuple[dict[str, object], str]:
        """``handler(body)`` on the write pool for ``device``, told when its request reached
        the server (``arrived``, ``time.monotonic``), and in this request's context otherwise.

        The thread asks the gates again first (:meth:`kit_write_still_allowed`), since the
        write may have waited for it. A device may have :data:`WRITE_WAITING_PER_DEVICE`
        writes waiting at once, and one more is 409 ``busy``: the machine is still busy with
        what that device sent, and no wait it could name in a ``Retry-After`` is known.
        """
        import asyncio

        waiting = [device.id]
        """Counted among the device's writes waiting for a thread, until it is not."""

        def stop_waiting() -> None:
            with self._lock:
                if waiting:
                    waiting.clear()
                    left = self._write_waiting.get(device.id, 1) - 1
                    if left > 0:
                        self._write_waiting[device.id] = left
                    else:
                        self._write_waiting.pop(device.id, None)

        def write_now() -> tuple[dict[str, object], str]:
            stop_waiting()
            self.kit_write_still_allowed(device)
            return handler(body)

        with self._lock:
            queued = self._write_waiting.get(device.id, 0)
            if queued >= WRITE_WAITING_PER_DEVICE:
                raise RequestError(
                    409,
                    "busy",
                    f"this device has {queued} writes waiting for the machine already — "
                    "send this once they are done",
                )
            self._write_waiting[device.id] = queued + 1
        try:
            context = contextvars.copy_context()
            context.run(_WRITE_ARRIVED.set, arrived)
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self.kit_write_pool(), context.run, write_now)
        finally:
            stop_waiting()  # it never ran: cancelled, or its pool shut down first

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

    async def kit_gated(
        self,
        request: Request,
        device: Device,
        endpoint: str,
        respond: Callable[[dict[str, Any]], Awaitable[Response]],
    ) -> Response:
        """A write-gated request, the write dispatcher's and a lane route's alike (SPEC
        §1.5): the body and its ``request_id``, the ledger, the write gate, then
        ``respond(body)`` once per ``request_id`` (:meth:`kit_ledgered`).

        A retry, its ``request_id`` known to the ledger with the same endpoint and body
        (:func:`_ledger_request`), is answered from it before the write gate is asked:
        the answer the first try got, or 409 ``in_progress`` while that still runs. A
        retry changes nothing, and asked first, the gate answered one 403 ``read_only``
        once writes were off: the page said "Read-only", greyed every write and settled
        the pending request with the 403, though the restart it was for had run (review
        of #243, round 3). Everything else needs the gate, and while writes are off a
        body the ledger cannot answer is a 403 whatever else is wrong with it. Past the
        gate, an id the ledger holds for another request is 409 ``request_id_reused``,
        not that request's answer: replayed, a stop sent with a send-keys' id was
        answered 200 and never ran (sweep of #243). A retry of a request whose answer
        the ledger no longer keeps, though it still knows the id, is 409
        ``already_answered``, saying how it ended, and never runs again.
        """
        from starlette.responses import JSONResponse

        allowed = self.kit_write_allowed()
        try:
            body = await self.kit_json_object(request)
            request_id = _ledger_request_id(body)
            asked = "" if request_id is None else _ledger_request(endpoint, body)
        except RequestError as exc:
            if not allowed:
                return self.kit_refuse(403, "read_only", READ_ONLY_REASON)
            return JSONResponse(exc.request_error_body(), status_code=exc.status)
        seen = None if request_id is None else self.ledger.ledger_seen(device.id, request_id, asked)
        if seen is not None and seen.same:
            if seen.spent is not None:
                answered = ALREADY_ANSWERED.format(request_id=request_id, status=seen.spent)
                return self.kit_refuse(409, "already_answered", answered)
            if seen.answer is None:
                return self.kit_refuse(409, "in_progress", IN_PROGRESS)
            status, payload = seen.answer
            return JSONResponse(payload, status_code=status)
        if not allowed:
            return self.kit_refuse(403, "read_only", READ_ONLY_REASON)
        if seen is not None:
            reused = REQUEST_ID_REUSED.format(request_id=request_id)
            return self.kit_refuse(409, "request_id_reused", reused)
        return await self.kit_ledgered(
            device, request_id, endpoint, lambda: respond(body), request=asked
        )

    async def kit_ledgered(
        self,
        device: Device,
        request_id: str | None,
        endpoint: str,
        respond: Callable[[], Awaitable[Response]],
        *,
        request: str = "",
    ) -> Response:
        """``respond()``, once per ``request_id`` (SPEC §1.5): the ledger flow of every
        write-gated request, once :meth:`kit_gated` found its id new to the ledger.

        Without an id it just runs. With one it is marked running as ``request`` (one
        that runs already is 409 ``in_progress``), and how it ended is stored, refusals
        included, so a retry gets the answer the first try got, and it is stored in
        a ``finally``: a crash or a cancellation is an ending too. The dispatcher kept
        a copy of this flow that stored after its ``try``, which stops an
        ``Exception`` and nothing else, so a cancelled write left its id running, and
        every retry of it was answered ``in_progress`` until the ledger forgot the id.
        """
        if request_id is None:
            return await respond()
        if not self.ledger.ledger_begin(device.id, request_id, endpoint, request):
            return self.kit_refuse(409, "in_progress", IN_PROGRESS)
        status, payload = 500, _error_body("internal_error", CRASHED)
        try:
            response = await respond()
            status, payload = response.status_code, _ledger_body(response)
            return response
        finally:  # even a crash is an ending: the id must never stay "running"
            self.ledger.ledger_finish(device.id, request_id, status, payload)

    def kit_route(
        self, path: str, endpoint: KitEndpoint, *, methods: list[str], write_gated: bool
    ) -> Route:
        """A lane's route: the endpoint gets the device and the parsed body (``{}`` for GET).

        With ``write_gated``, a POST, PUT or DELETE goes through :meth:`kit_gated`:
        it takes the optional ``request_id`` out of the body, answers a retried id from
        the ledger instead of running again (one still running is 409
        ``in_progress``), answers 403 ``read_only`` while writes are off, and stores
        how every request ended, refusals included, so a retry gets the answer the
        first try got.

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
            try:
                device = self.kit_device(request)
            except RequestError as exc:
                return JSONResponse(exc.request_error_body(), status_code=exc.status)
            if write_gated and not reading:  # a read never waits on the write gate
                return await self.kit_gated(
                    request, device, name, lambda body: kit_respond(request, device, body)
                )
            try:
                body = {} if reading else await self.kit_json_object(request)
            except RequestError as exc:
                return JSONResponse(exc.request_error_body(), status_code=exc.status)
            return await kit_respond(request, device, body)

        return Route(path, kit_endpoint, methods=methods)


@contextlib.asynccontextmanager
async def remote_lifespan(kit: RemoteKit) -> AsyncIterator[None]:
    """The lanes' background work starts with the server and stops with it.

    The needs watcher first, then the push sender, which listens to it; at
    shutdown their stoppers run in reverse, and then the pane and write pools
    are shut down. A lane that fails to start costs its own feature and never the
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
        for pool in (kit.pane_pool, kit.write_pool):
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
        kit.pane_pool = kit.write_pool = None


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
        from starlette.exceptions import HTTPException
        from starlette.responses import FileResponse, JSONResponse
        from starlette.routing import Mount, Route, WebSocketRoute
        from starlette.status import WS_1011_INTERNAL_ERROR
        from starlette.websockets import WebSocketDisconnect
    except ImportError as exc:  # pragma: no cover - exercised only in a base install
        raise RemoteUnavailable(
            f"the remote extra is not installed — {remote_install_hint()}"
        ) from exc

    from aisquare.services import remote_actions, remote_needs, remote_push

    reads = sources or live_sources()
    board_frame = reads.board_frame or (lambda project: remote_board_frame(reads.board(project)))
    handlers = {
        name: _remote_write_tracked(name, handler)
        for name, handler in (writes or live_writes()).handlers.items()
    }
    dist = (dist_dir or remote_dist_dir()).resolve()
    limiter = _RateLimiter(clock)
    budget = UnlockBudget(runtime)
    cache = _Cache(ttl=tick * 0.9)
    kit = RemoteKit(runtime, tick=tick)

    def cookie_path(request: Request) -> str:
        return f"/r/{request.path_params['token']}"

    async def snapshot(kind: str, compute: Snapshot) -> object:
        return await cache.cached_snapshot(kind, compute)

    def remote_pane_frame(label: str, project: str) -> dict[str, object]:
        """A pane subscription's live frame: the capture, or what stopped it (``error``).

        §4-L: history is a FETCH, live stays a stream, so 0 keeps this frame the
        live shape, with no history keys. A failure is a frame too, so the sockets
        watching a pane that is gone share its answer as they share a capture.
        """
        try:
            return reads.panes(label, project or None, 0)
        except Exception as exc:
            return {"rows": [], "width": 0, "height": 0, "error": str(exc)}

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
        password: str, ua: str, cookies: list[str], direct: bool
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
            known = next(
                (found for found in map(runtime.known_device_for_cookie, cookies) if found), None
            )
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
                    kit.kit_audit(known, "unlock", revoked)
                return None
            secret, device = unlocked
            reactivated = known is not None and device.id == known.id
            summary = f"device {device.id} " + ("reactivated" if reactivated else f"ua={ua[:60]}")
            kit.kit_audit(device, "unlock", summary)
            return secret, device, reactivated

    async def unlock_endpoint(request: Request) -> Response:
        """``POST api/unlock``: the passphrase for a cookie (SPEC §2.2).

        In order: the per-client limiter (every unlock); then, unless this is a
        phone that unlocked here before (a cookie it sends names a known device) or the
        machine itself, the global failed-unlock budget, which refuses a spent
        budget WITHOUT evaluating the guess; then the passphrase. A wrong one counts
        against the known device's own cap, or against the budget (the machine's
        never does). A right one reactivates the known device under its old id, or
        makes a new one. Everything after the body is :func:`unlock_decision`'s.
        """
        retry = limiter.limiter_wait_seconds(
            _client_of(request.scope), UNLOCK_LIMIT, UNLOCK_WINDOW_SECONDS
        )
        if retry is not None:
            return kit.kit_refuse(
                429,
                "too_many_attempts",
                f"{UNLOCK_LIMIT} attempts a minute — wait",
                headers={"Retry-After": str(retry)},
            )
        try:
            body = await kit.kit_json_object(request)
        except RequestError:
            body = {}
        password = body.get("password")
        if not isinstance(password, str):
            return _json_error(400, "invalid", 'send {"password": "..."}')
        try:
            decided = await asyncio.to_thread(
                unlock_decision,
                password,
                request.headers.get("user-agent", ""),
                remote_cookie_values(request.scope),
                is_direct_loopback(request.scope),
            )
        except RequestError as exc:  # Remote went off as this unlock waited its turn
            return JSONResponse(exc.request_error_body(), status_code=exc.status)
        except OSError as exc:  # its device was taken back (Runtime._write_state's undo)
            log.warning("remote: an unlock could not be saved: %s", exc)
            return kit.kit_refuse(503, "remote_state_unwritable", STATE_UNWRITABLE)
        if isinstance(decided, datetime):
            wait = max(1, math.ceil((decided - _remote_now()).total_seconds()))
            return kit.kit_refuse(429, "locked_out", LOCKED_OUT, headers={"Retry-After": str(wait)})
        if decided is None:
            return _json_error(401, "wrong_password", WRONG_PASSWORD)
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
        The thread that extends asks the gates again first
        (:meth:`RemoteKit.kit_write_still_allowed`): it may have waited for the pool.
        """

        def extend_now() -> datetime | None:
            kit.kit_write_still_allowed(device)
            return runtime.extend_auto_off(_remote_now())

        try:
            extended = await asyncio.to_thread(extend_now)
        except OSError as exc:  # the deadline was put back (Runtime._write_state's undo)
            log.warning("remote: an extend could not be saved: %s", exc)
            return kit.kit_refuse(503, "remote_state_unwritable", STATE_UNWRITABLE)
        if extended is None:
            return kit.kit_refuse(409, "no_auto_off", "Remote has no auto-off deadline to extend")
        stamp = _iso_seconds(extended)
        await asyncio.to_thread(
            kit.kit_audit, device, "remote/extend", f"extend auto_off_at={stamp}"
        )
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
        The revoke writes ``remote.json`` in a worker thread, as an unlock does, and
        that thread asks the gates again first for another device's
        (:meth:`RemoteKit.kit_write_still_allowed`): it may have waited for the pool.
        """
        device = kit.kit_device(request)
        device_id = request.path_params["device_id"]
        own = device_id == device.id
        if not own and not DEVICE_ID.fullmatch(device_id):
            return kit.kit_refuse(404, "not_found", "no such device")
        if not own and not kit.kit_write_allowed():
            return kit.kit_refuse(403, "read_only", READ_ONLY_REASON)

        def revoke_now() -> bool:
            if not own:
                kit.kit_write_still_allowed(device)
            return runtime.revoke_device(device_id)

        try:
            revoked = await asyncio.to_thread(revoke_now)
        except RequestError as exc:
            return JSONResponse(exc.request_error_body(), status_code=exc.status)
        except OSError as exc:  # it holds in memory, where the gate reads it: recorded
            log.warning("remote: a revoke could not be saved: %s", exc)
            target = "self" if own else device_id
            await asyncio.to_thread(kit.kit_audit, device, "devices/revoke", f"{target} unsaved")
            unsaved = REVOKE_UNSAVED.format(device=device_id)
            return kit.kit_refuse(503, "remote_state_unwritable", unsaved)
        if not revoked:
            return kit.kit_refuse(404, "not_found", "no such device")
        await asyncio.to_thread(
            kit.kit_audit, device, "devices/revoke", "self" if own else device_id
        )
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
        except RequestError as exc:  # its pane id is another agent's now: 409 not_agent
            return JSONResponse(exc.request_error_body(), status_code=exc.status)
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
        except RequestError as exc:  # a cursor of another conversation: 409 stale_cursor
            return JSONResponse(exc.request_error_body(), status_code=exc.status)
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

        In order: the name, then what every write-gated lane route goes through too
        (:meth:`RemoteKit.kit_gated`): the body and its optional ``request_id``, a
        retry answered from the ledger, the write gate, and the handler on the write
        pool (:meth:`RemoteKit.kit_run_write`), its ending stored, refusals as well, so
        a retry gets the same refusal. Last, the audit line for a write that went
        through.
        """
        arrived = time.monotonic()
        device = kit.kit_device(request)
        name = request.path_params["name"]
        handler = handlers.get(name) if name in write_endpoint_names() else None
        if handler is None:
            return _json_error(404, "not_found", f"there is nothing to write at api/{name}")
        summary: str | None = None

        async def dispatched(body: dict[str, Any]) -> Response:
            nonlocal summary
            try:
                result, summary = await kit.kit_run_write(handler, body, arrived, device)
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
            return JSONResponse(payload, status_code=status)

        response = await kit.kit_gated(request, device, name, dispatched)
        # After the ledger has the ending, and best effort (RemoteKit.kit_audit): a write
        # that went through must not read as failed for its line. In a worker thread: the
        # first line creates the log and restricts it to this account, on Windows an
        # icacls run, and every line opens and appends to a file.
        if summary is not None:
            await asyncio.to_thread(kit.kit_audit, device, name, summary)
        return response

    async def api_missing(request: Request) -> Response:
        rest = request.path_params["rest"]
        return _json_error(404, "not_found", f"there is nothing to read at api/{rest}")

    async def wrong_method(request: Request, exc: Exception) -> Response:
        """Starlette's 405, for a route asked with a method it does not take, in the one
        shape: its own answer was ``Method Not Allowed`` as plain text.

        The sentence says what ``Allow`` says, the methods of the route that matched,
        and names no path: ``DELETE api/nuke`` matches the write catch-all, which takes
        ``POST``, though a ``POST`` there is a 404.
        """
        headers = exc.headers if isinstance(exc, HTTPException) else None
        named = (headers or {}).get("Allow", "").split(",")
        allowed = ", ".join(sorted(method.strip() for method in named if method.strip()))
        said = f"this route does not take {request.method} — it takes {allowed}"
        response = _json_error(405, "method_not_allowed", said)
        response.headers["Allow"] = allowed
        return response

    async def static(request: Request) -> Response:
        """The page: ``--dist``, else the installed build, else the one aisquare-cli bundles.

        Decided per request, so ``install-page`` takes over from the bundled page,
        and removing what it installed hands back, without a restart. Every answer
        carries the page headers (no referrer: the token is in the path); only the
        bundled page also gets its CSP, since an installed build may need another.
        Decided in a worker thread: it asks the disk (the installed index, the file's
        path resolved), and the first answer reads the bundled page's files, which on
        the event loop held up every request and socket it serves.
        """
        return await asyncio.to_thread(page_answer, request)

    def page_answer(request: Request) -> Response:
        """:func:`static`'s answer. An installed build's file is typed by the page's own
        closed list (:func:`remote_page.build_content_type`), not the machine's tables."""
        from aisquare.services import remote_page

        rel = request.path_params.get("path", "")
        index = dist / "index.html"
        if dist_dir is None and not index.is_file():
            bundled = remote_page.bundled_page_response(rel, request)
            if bundled is not None:
                return bundled
            response = _json_error(404, "no_dist", NO_PAGE_HINT)
        elif rel and (candidate := _built_page_file(dist, rel)) is not None:
            response = FileResponse(
                candidate,
                media_type=remote_page.build_content_type(candidate.name),
                headers={"cache-control": _cache_control(rel)},
            )
        elif rel and not _is_navigation(rel, request.headers.get("accept", "")):
            # A file was asked for and there is no such file. Saying so is the
            # whole point: the SPA document under a .js name is a boot failure
            # with no error, and the 200 hides which build is actually installed.
            response = _json_error(404, "not_found", f"no such file in the built page: {rel}")
        elif index.is_file():
            response = FileResponse(
                index,
                media_type=remote_page.build_content_type(index.name),
                headers={"cache-control": INDEX_CACHE_CONTROL},
            )
        else:
            response = _json_error(404, "no_dist", f"no index.html in {dist}")
        response.headers.update(remote_page.remote_page_headers())
        return response

    async def stream(websocket: WebSocket) -> None:
        """``/ws``: every tick, each frame that changed (SPEC §1.6).

        In order: ``board`` and ``fleet`` (each only to a socket that asked, with
        ``subscribe_board`` and ``subscribe_fleet``), ``remote``, then ``needs_you``
        and ``action``, then the ``heartbeat`` (every ``heartbeat`` seconds, changed
        or not, never on the first tick), then one ``pane`` frame per subscription. Pane
        subscriptions are ``(project, label)``: the same label in two projects is
        two agents, and a frame names the project its subscription named. A
        ``board`` frame names its subscription's project too: the board it carries
        may be another project's, since ``AISQUARE_TEAM_HUB`` makes every project's
        board the hub's, and that board names the hub's project. A lane seam that
        raises skips its own frame for the tick; anything else that fails ends the
        socket with 1011.

        A tick reads its snapshots (board, fleet, each pane) at once and waits a tick at
        most for them. A snapshot still being read after that sends its frame on a later
        tick, and the frames that need no read (``remote``, ``needs_you``, ``action``,
        the heartbeat) and the snapshots that came back go out without it.

        A tick is one tick, the wait for its reads included: the next begins a tick after
        this one began. The wait for the reads and the pause after the frames each took a
        whole tick, so while one read hung (a fleet waiting out tmux's 30 s, a store kept
        busy) every frame of the socket came every two ticks, and an auto-off or a device
        signed out elsewhere closed it up to two ticks late (sweep of #243, round 5).
        """
        device = kit.kit_device(websocket)  # the gate refused a socket without one
        await websocket.accept()
        loop = asyncio.get_running_loop()
        panes_wanted: dict[tuple[str, str], object] = {}
        """``(project ref, label)`` per pane subscription, oldest first (``""`` is the CURRENT
        project), to the payload of the last ``pane`` frame it was sent (``None`` before the
        first). A dict for its order: the frames of a tick follow the order subscriptions
        came in, and a capture that took longer than the tick's wait sends its frame on a
        later tick. The last frame lives WITH its subscription, so unsubscribing forgets
        both: a socket that cycles through labels holds what its 8 subscriptions hold, and
        no more."""
        fleet_project: str | None = None
        """``None`` = the CURRENT project; a ``{subscribe_fleet: "<project>"}`` text frame
        picks another one's ``fleet`` frames (``""``/``null`` returns). The frame shape
        does not change, only WHICH project's ``fleet ls`` payload fills it."""
        fleet_wanted = False
        """No ``fleet`` frame goes out until the socket asks with ``subscribe_fleet``, and
        ``{subscribe_fleet: false}`` stops them. Each is a ``fleet ls``: tmux on the
        project's socket, the store, and a write for a pane found dead. Every socket was
        read one every tick from the moment it opened, a phone on the Needs screen too,
        though only the page's project and agent screens draw it (review of #243, round
        4)."""
        board_project: str | None = None
        """The same, for ``board`` frames and ``{subscribe_board: "<project>"}``."""
        board_wanted = False
        """No ``board`` frame goes out until the socket asks with ``subscribe_board``, and
        ``{subscribe_board: false}`` stops them. A board is every session and task its
        project ever had, and it changes with every session's heartbeat: sent to every
        socket, it reached each phone several times a minute, whatever screen it showed,
        and only the page's Board tab draws it."""
        last: dict[str, object] = {}
        """The payload of the last frame of every other kind (:func:`_same_frame`), keyed by the
        kind alone and never by a string the client sent, so it cannot grow with what a
        client sends. Switching projects forgets that kind's frame, so the new project's
        goes out even if equal."""
        pending: dict[str, asyncio.Future[object]] = {}
        """The snapshots this socket is reading, by their cache kind (:class:`_Cache`): one a
        tick's wait did not see come back stays here, and a later tick takes what it came
        to. At most the board, the fleet and the 8 panes the socket wants."""
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

        async def push_if_changed(
            kind: str, payload: object, *, project: str | None = None
        ) -> None:
            if not _same_frame(last.get(kind, _UNSENT), payload):
                last[kind] = payload
                await send_frame(kind, payload, project=project)

        def lane_frame_skipped(seam: str) -> None:
            """Called from an ``except``: a lane's bug costs its own frame, never the socket."""
            level = logging.DEBUG if seam in lanes_failing else logging.WARNING
            lanes_failing.add(seam)
            log.log(level, "remote: %s failed; the stream goes on without it", seam, exc_info=True)

        def remote_read(
            kind: str, compute: Snapshot, pool: Executor | None = None
        ) -> asyncio.Future[object]:
            """``kind``'s snapshot as this socket reads it: the read an earlier tick began and
            nothing has taken yet, or a new one, in a task of its own."""
            began = pending.get(kind)
            if began is None:
                began = pending[kind] = asyncio.ensure_future(
                    cache.cached_snapshot(kind, compute, pool)
                )
            return began

        def remote_taken(kind: str | None) -> object:
            """What ``kind``'s read came to (:func:`_read_outcome`), taken out of ``pending``;
            :data:`_UNREAD` while it is still being read, and for no kind."""
            began = None if kind is None else pending.get(kind)
            if kind is None or began is None or not began.done():
                return _UNREAD
            del pending[kind]
            return _read_outcome(began)

        async def tick_once(ends: float) -> None:
            nonlocal next_heartbeat, first_tick
            # Every snapshot is read at once and waited for a tick at most. Read in turn and
            # awaited, one that hung held every frame behind it: a tmux that stops answering
            # waits out its 30 s, and the heartbeat, needs-you and every other pane waited
            # too, so the page took a live link for a dead one and held every button (sweep
            # of #243, round 4). What the socket wants is looked at again after the wait: a
            # switch that landed meanwhile must not let the old project's frame out, which
            # the page would show as the new one's.
            board_ref, fleet_ref = board_project, fleet_project
            board_kind = f"board-frame:{board_ref or ''}" if board_wanted else None
            fleet_kind = f"fleet:{fleet_ref or ''}" if fleet_wanted else None
            in_flight: list[asyncio.Future[object]] = []
            if board_kind is not None:
                in_flight.append(remote_read(board_kind, lambda: board_frame(board_ref)))
            if fleet_kind is not None:
                in_flight.append(remote_read(fleet_kind, lambda: reads.fleet(fleet_ref)))
            # One capture per pane per tick however many sockets watch it, as for board and
            # fleet, and on the pane pool (§2.10). The key is the pair as JSON: a ':' in a ref
            # or a label must not make two pairs one kind.
            pane_kinds = {wanted: "pane:" + json.dumps(list(wanted)) for wanted in panes_wanted}
            for (project, label), kind in pane_kinds.items():
                capture = functools.partial(remote_pane_frame, label, project)
                in_flight.append(remote_read(kind, capture, kit.kit_pane_pool()))
            wanted_kinds = {board_kind, fleet_kind, *pane_kinds.values()}
            for kind in [kind for kind in pending if kind not in wanted_kinds]:
                _let_read_go(pending.pop(kind))  # switched away from, or unsubscribed
            if not all(began.done() for began in in_flight):
                await asyncio.wait(in_flight, timeout=max(0.0, ends - loop.time()))
            board = remote_taken(board_kind)
            if board is not _UNREAD and board_wanted and board_ref == board_project:
                if isinstance(board, Exception):  # said on the Board tab, not "Loading…" for good
                    log.debug("remote: board frame unread: %s", board)
                    board = remote_board_unread(board)
                await push_if_changed("board", board, project=board_ref or None)
            fleet = remote_taken(fleet_kind)
            if isinstance(fleet, Exception):
                log.debug("remote: fleet frame skipped: %s", fleet)
            elif fleet is not _UNREAD and fleet_wanted and fleet_ref == fleet_project:
                await push_if_changed("fleet", fleet)
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
            for wanted, kind in pane_kinds.items():
                payload = remote_taken(kind)
                if payload is _UNREAD:
                    continue  # still being captured: its frame goes out on a later tick
                if isinstance(payload, Exception):  # the pool, shut down under a socket ticking
                    payload = {"rows": [], "width": 0, "height": 0, "error": str(payload)}
                # Not wanted any more: unsubscribed while the capture ran, so no frame,
                # and nothing kept for it either.
                if wanted in panes_wanted and not _same_frame(panes_wanted[wanted], payload):
                    panes_wanted[wanted] = payload
                    project, label = wanted
                    await send_frame("pane", payload, agent=label, project=project or None)

        async def reader() -> None:
            nonlocal fleet_project, fleet_wanted, board_project, board_wanted
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
                if ref is not None and not isinstance(ref, str):
                    continue  # a project that is no name names none, never the CURRENT one
                project = ref or ""
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
                    fleet_project, fleet_wanted = target or None, True
                    last.pop("fleet", None)
                elif target is False and "subscribe_fleet" in message:
                    fleet_wanted = False  # sent false: no fleet frames from now on
                    last.pop("fleet", None)
                target = message.get("subscribe_board", False)
                if target is None or isinstance(target, str):
                    board_project, board_wanted = target or None, True
                    last.pop("board", None)
                elif target is False and "subscribe_board" in message:
                    board_wanted = False  # sent false: no board frames from now on
                    last.pop("board", None)

        runtime.register_socket(device.id, closer)
        kit.kit_socket_opened(device.id, closer)
        reading = asyncio.ensure_future(reader())
        try:
            while not reading.done():
                ends = loop.time() + tick
                # One check of remote.json a tick, for everything the tick reads of it.
                with runtime.remote_state_checked():
                    # By id, every tick: Remote off (auto-off included) is 4410, a device that
                    # is gone, expired or idle past the limit is 4401, whatever the cookie said.
                    if runtime.auto_off_passed(_remote_now()):
                        await close_with(WS_CLOSE_REMOTE_OFF)
                        break
                    if not runtime.device_is_live(device.id):
                        await close_with(WS_CLOSE_UNAUTHORIZED)
                        break
                    await tick_once(ends)
                await asyncio.wait([reading], timeout=max(0.0, ends - loop.time()))
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
            for began in pending.values():
                _let_read_go(began)
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
        routes=[Mount("/r/{token}", routes=api_routes)],
        exception_handlers={405: wrong_method},
        lifespan=lambda app: remote_lifespan(kit),
    )
    return _TokenGate(inner, runtime, kit)


build_app = build_remote_app
"""``build_app`` is the name every caller uses (SPEC §1). The def carries its area prefix
because #240 defines a ``build_app`` of its own that the hook path reaches by bare name
(``serve`` → the captain's voice ``serve`` → ``build_app``), and a remote def of that
name would pull this whole app into the graph the config-write guard walks. An
assignment is not a def, so the alias bridges nothing."""


# --- process lifecycle: the module API the TUI modal calls (PLAN §4-F) ----------------


def remote_install_hint() -> str:
    """The one command that adds the ``remote`` extra to the install that is running, made
    the way that install was made.

    It said ``pip install 'aisquare-cli[remote]' (or: pipx inject aisquare-cli
    websockets)``, and neither fixed the install the docs give, a uv tool
    (``install.sh``, the README): its environment has no pip, and there is no pipx
    environment to inject into. Nor did the inject fix a pipx install made without
    the extra, which misses starlette and uvicorn too: the same sentence came back
    after it.

    A uv tool is installed again as its receipt says it was, the extra added
    (:func:`_remote_uv_tool_hint`): uv has no inject. A pipx install gets what is
    missing injected, which keeps what was injected before. A virtualenv, or any
    other Python, gets the extra from its own interpreter: ``uv pip`` when uv made
    it, since it has no pip, else ``-m pip``. Each word is quoted for the shells of
    this platform (:func:`_remote_shell_word`). An interpreter whose Windows path needs
    quotes cannot come first as it is in both of that platform's shells: PowerShell reads
    a quoted first word as a string, not a program, and needs ``&`` before it, which
    cmd.exe refuses. That command is PowerShell's, saying what cmd.exe drops.
    """
    import importlib.util
    import sys

    from aisquare.core.version import DISTRIBUTION

    prefix = Path(sys.prefix)
    if (prefix / "uv-receipt.toml").is_file():
        return _remote_uv_tool_hint(prefix / "uv-receipt.toml")
    if (prefix / "pipx_metadata.json").is_file():
        missing = [name for name in REMOTE_EXTRA if importlib.util.find_spec(name) is None]
        return f"pipx inject {DISTRIBUTION} {' '.join(missing or REMOTE_EXTRA)}"
    python = _remote_shell_word(sys.executable)
    extra = _remote_shell_word(f"{DISTRIBUTION}[remote]")
    if _remote_made_by_uv(prefix):
        return f"uv pip install --python {python} {extra}"
    if sys.platform == "win32" and python.startswith('"'):
        return f"& {python} -m pip install {extra} (in cmd.exe, without the &)"
    return f"{python} -m pip install {extra}"


_UV_SOURCES = ("url", "path", "directory", "editable", "git")
"""The keys a uv receipt gives a requirement's source by, when an index is not it."""
_UV_REQUIREMENT_KEYS = frozenset(
    {"name", "extras", "specifier", "marker", "subdirectory", *_UV_SOURCES}
)
"""What :func:`_remote_uv_requirement` can say again: a requirement with any other key is one
it cannot say whole."""
_UV_GIT_REFS = ("rev", "branch", "tag")
"""How a receipt's ``git`` URL names the reference asked for, in its query."""
_UV_FROM_FILES = ("constraints", "overrides", "build-constraint-dependencies", "excludes")
"""What a receipt records from files (``-c``, ``--overrides``, ``-b``, ``--excludes``), which
no command line carries."""


def _remote_uv_tool_hint(receipt: Path) -> str:
    """``uv tool install`` as this tool's receipt says it was installed, ``remote`` added to
    aisquare-cli's extras.

    Installing a tool again resolves it from that command alone (measured with uv
    0.12.19): what the command does not name goes. A fixed ``--with tiktoken
    'aisquare-cli[remote]'`` took the ``serve`` extra's mcp, and ``aisquare serve``
    with it, from a tool installed with ``[serve]``, and any other ``--with``. So the
    command names all the receipt records: each requirement as it was given (a pin, a
    marker, a git or local source, editable or not), the packages whose executables
    it took, and the Python: the receipt's, else the one it runs on, since without
    ``--python`` uv takes its own default and makes the tool anew on another. A
    receipt that cannot be read gets the command ``install.sh`` installs with; one
    that records what no command line carries, a constraints file or a requirement in
    a shape this does not know, gets a sentence naming it rather than a command that
    would drop it.
    """
    import sys
    import tomllib

    from aisquare.core.version import DISTRIBUTION

    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    try:
        tool = tomllib.loads(receipt.read_text(encoding="utf-8"))["tool"]
        requirements = tool["requirements"]
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError):
        tool, requirements = {}, None
    if not isinstance(requirements, list) or not requirements:
        as_installed = ["--python", running, "--with", "tiktoken", f"{DISTRIBUTION}[remote]"]
        return " ".join(
            _remote_shell_word(word) for word in ["uv", "tool", "install", *as_installed]
        )
    said = _remote_uv_tool_words(tool, requirements, running)
    if said is None:
        return (
            f"install {DISTRIBUTION} again with uv tool install, as {receipt} records it, "
            "adding remote to its extras"
        )
    return " ".join(_remote_shell_word(word) for word in said)


def _remote_uv_tool_words(
    tool: dict[str, Any], requirements: list[Any], running: str
) -> list[str] | None:
    """The words of :func:`_remote_uv_tool_hint`'s command; ``None`` when they would drop
    something the receipt records."""
    from aisquare.core.version import DISTRIBUTION

    python, points = tool.get("python", running), tool.get("entrypoints", [])
    main, *others = requirements
    if (
        not isinstance(python, str)
        or not isinstance(points, list)
        or any(tool.get(key) for key in _UV_FROM_FILES)
        or not isinstance(main, dict)
        or main.get("name") != DISTRIBUTION
    ):
        return None
    froms = [point.get("from") for point in points if isinstance(point, dict)]
    executables = {name for name in froms if isinstance(name, str)}
    words = ["uv", "tool", "install", "--python", python]
    for entry in others:
        given = _remote_uv_requirement(entry)
        if given is None:
            return None
        editable, requirement = given
        flag = "--with-executables-from" if entry["name"] in executables else "--with"
        words += ["--with-editable" if editable else flag, requirement]
    given = _remote_uv_requirement(main, extra="remote")
    if given is None:
        return None
    editable, requirement = given
    return [*words, *(["--editable"] if editable else []), requirement]


def _remote_uv_requirement(entry: object, *, extra: str = "") -> tuple[bool, str] | None:
    """A receipt's requirement as a command gives it, ``extra`` added to its extras: whether
    it is editable, and the requirement; ``None`` for one it cannot give whole.

    The receipt's shape is uv's own (``RequirementWire``): a name, extras, a marker, and
    a specifier for a package from an index, else one source, given back as a direct
    reference.
    """
    if not isinstance(entry, dict) or not entry.keys() <= _UV_REQUIREMENT_KEYS:
        return None
    extras = entry.get("extras", [])
    if not isinstance(extras, list) or "name" not in entry:
        return None
    fields = [value for key, value in entry.items() if key != "extras"]
    if not all(isinstance(value, str) for value in [*fields, *extras]):
        return None
    at = _remote_uv_source(entry)
    if at is None:
        return None
    named = sorted({*extras, extra} - {""})
    requirement = entry["name"] + (f"[{','.join(named)}]" if named else "") + at
    if "marker" in entry:
        requirement += f" ; {entry['marker']}"
    return "editable" in entry, requirement


def _remote_uv_source(entry: dict[str, Any]) -> str | None:
    """What follows a receipt requirement's name and extras: its specifier, or `` @ `` and
    its source as a direct reference; ``None`` for a source it cannot give whole."""
    from urllib.parse import parse_qsl, urlsplit, urlunsplit

    sources = [key for key in _UV_SOURCES if key in entry]
    subdirectory = entry.get("subdirectory", "")
    if not sources:
        return None if subdirectory else entry.get("specifier", "")
    kind = sources[0]
    if len(sources) > 1 or "specifier" in entry or (subdirectory and kind != "url"):
        return None
    if kind == "url":
        if subdirectory and "#" in entry["url"]:
            return None
        return f" @ {entry['url']}" + (f"#subdirectory={subdirectory}" if subdirectory else "")
    if kind == "git":
        try:
            url = urlsplit(entry["git"])
        except ValueError:  # an IPv6 host left open, say: no URL uv writes
            return None
        query = dict(parse_qsl(url.query))
        refs = [f"@{query.pop(key)}" for key in _UV_GIT_REFS if key in query]
        subdirectory = query.pop("subdirectory", "")
        if query or len(refs) > 1:
            return None
        repository = urlunsplit((*url[:3], "", "")).removeprefix("git+")
        fragment = f"#subdirectory={subdirectory}" if subdirectory else ""
        return f" @ git+{repository}{''.join(refs)}{fragment}"
    try:
        return f" @ {Path(entry[kind]).as_uri()}"
    except ValueError:  # a relative path, which a receipt does not record
        return None


_WINDOWS_BARE_WORD = re.compile(r"[\w.:\\/-]+")
"""A word cmd.exe and PowerShell both pass on as it is."""


def _remote_shell_word(word: str) -> str:
    """``word`` quoted for the shells a human types the hint into on this platform.

    POSIX quoting is wrong on Windows (``core.agents._quote`` says how for a hook's
    path): cmd.exe has no single quotes and passes them on, so ``'aisquare-cli[remote]'``
    reached pip quotes and all, and ``shlex.quote`` wraps every Windows path, whose
    ``\\`` it counts unsafe, in them. There a word is bare when cmd.exe and
    PowerShell both read it as it is, and double-quoted otherwise: a space, a ``,`` or
    ``;`` (PowerShell's), a ``<`` or ``>`` (cmd.exe's), an extra's brackets.
    """
    import shlex
    import sys

    if sys.platform != "win32":
        return shlex.quote(word)
    return word if _WINDOWS_BARE_WORD.fullmatch(word) else f'"{word}"'


def _remote_made_by_uv(prefix: Path) -> bool:
    """Whether uv made the virtualenv at ``prefix``: its ``pyvenv.cfg`` names uv's version."""
    try:
        lines = (prefix / "pyvenv.cfg").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    return any(line.partition("=")[0].strip() == "uv" for line in lines)


def _remote_dependency_error() -> str | None:
    import importlib.util

    missing = [name for name in REMOTE_SERVER_NEEDS if importlib.util.find_spec(name) is None]
    if not missing:
        return None
    named = ", ".join(missing)
    return f"the remote extra is not installed ({named} missing) — {remote_install_hint()}"


def runtime() -> Runtime:
    """The process-wide state, loaded from ``remote.json`` on first use."""
    global _runtime
    with _lock:
        if _runtime is None:
            ensure_home()
            _runtime = Runtime(remote_state_path(), remote_audit_path())
        return _runtime


def remote_state_loaded() -> bool:
    """Whether this process has read ``remote.json`` (:func:`runtime`): its first read makes
    the file when it is missing, and rewrites one older or edited by hand, under the file's
    lock (:meth:`Runtime._load_state`); every later read only reads."""
    return _runtime is not None


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

    @property
    def winding_down(self) -> bool:
        """Told to stop, and still finishing what was asked of it: the requests in flight,
        then the lanes, then the threads of its default pool."""
        return self._server.should_exit and self._thread.is_alive()

    def wound_down(self, timeout: float | None) -> bool:
        """Wait at most ``timeout`` s (``None``: as long as it takes) for it to finish
        stopping; whether it has."""
        self._thread.join(timeout)
        return not self._thread.is_alive()


_winding_down: list[_Server] = []
"""Servers :func:`stop_remote_server` stopped that were still finishing what was asked of them,
from the moment they were told to stop: for :func:`remote_wait_for_writes` to see out, for the
home's claim to outlast (:func:`_release_remote_home`), and for no new server of this process
to start beside (:func:`start_remote_server`)."""

REMOTE_WINDING_DOWN_SECONDS = 5.0
"""How long :func:`remote_wait_for_writes` gives a stopped server once its writes are done: the
answers go out, then the lanes stop, as :meth:`_Server.stop_serving` allows."""

_WRITE_TARGET = re.compile(r"[\w.@-]{1,64}\Z")
"""An agent named in a write's body that may be printed to the terminal: a label, never a
control character a phone sent."""
_writes_lock = threading.RLock()
"""Re-entrant: ``serve``'s signal handler reads the writes, and Python runs a handler in the
main thread between two bytecodes, so a second Ctrl-C's can land while the first's holds it."""
_writes: dict[object, str] = {}
"""What phones asked for that a worker thread is doing now, oldest first: the writes quitting
waits for (:func:`remote_wait_for_writes`, :func:`_remote_serve_server`)."""


@contextlib.contextmanager
def _remote_write_running(what: str) -> Iterator[None]:
    """Count ``what`` among the writes running (:data:`_writes`) for as long as it runs."""
    ticket = object()
    with _writes_lock:
        _writes[ticket] = what
    try:
        yield
    finally:
        with _writes_lock:
            del _writes[ticket]


def _remote_write_tracked(name: str, handler: WriteHandler) -> WriteHandler:
    """``handler``, counted among the writes running while its worker thread runs it.

    In the thread, not around the request: uvicorn's shutdown can end a request whose
    thread goes on, and that thread is what the process waits for at exit.
    """

    def tracked_write(body: dict[str, Any]) -> tuple[dict[str, object], str]:
        agent = body.get("agent")
        named = isinstance(agent, str) and _WRITE_TARGET.fullmatch(agent) is not None
        with _remote_write_running(f"{name} for {agent}" if named else name):
            return handler(body)

    return tracked_write


def remote_writes_running() -> list[str]:
    """The writes phones asked for that are still running in this process, oldest first:
    the endpoint, and the agent it is for (``agent/restart for coder-1``)."""
    with _writes_lock:
        return list(_writes.values())


def _remote_say(line: str) -> None:
    """One line on stderr, written once and unbuffered: it may be said from a signal handler,
    which must not re-enter a write the interrupted code was making.

    In the encoding the terminal reads (a Windows console's code page), else in
    ``sys.stderr``'s: UTF-8 bytes written past ``sys.stderr`` showed an agent's ``é`` as
    two other characters on a legacy console. A stderr that is gone loses the line,
    never the shutdown saying it.
    """
    import sys

    encoding = os.device_encoding(2) or getattr(sys.stderr, "encoding", None) or "utf-8"
    with contextlib.suppress(OSError, LookupError):
        os.write(2, f"{line}\n".encode(encoding, errors="replace"))


def _remote_writes_announced(ctrl_c: str) -> bool:
    """Say which writes the way out waits for, and that ``ctrl_c`` quits at once instead;
    whether any is running."""
    running = remote_writes_running()
    if running:
        _remote_say(
            f"waiting for {', '.join(running)} to finish (a restart or switch can take 40 s); "
            f"{ctrl_c} quits now and leaves it unfinished"
        )
    return bool(running)


REMOTE_QUIT_PUSH_SECONDS = 2.0
"""How long quitting at once (:func:`_remote_quit_now`) still gives the one-shot pushes in
flight, such as the farewell ``serve``'s auto-off queued just before: the exit it skips would
have given them ``remote_push.PUSH_DRAIN_SECONDS``."""

_quitting = False
"""Set once :func:`_remote_quit_now` has begun. A plain flag, read and set without a lock: a
Ctrl-C's handler may run it again while it waits for a push."""


def _remote_quit_now() -> NoReturn:
    """End the process at once, saying which writes it leaves unfinished.

    At once is the point: Python's own exit waits for every worker thread, the ones
    still running a write included (``concurrent.futures`` joins them all), so an exit
    any other way waits as long as the write does, and a Ctrl-C at that point only
    prints a traceback. What that exit runs is skipped with it. ``serve``'s
    ``remote.json`` cleanup: the next Remote sets its own deadline, and the devices'
    expiry bounds them. And the wait that lets a one-shot push in flight arrive
    (``remote_push.push_drain``), which runs here instead, for
    :data:`REMOTE_QUIT_PUSH_SECONDS` at most, so the farewell an auto-off sent still
    goes out; one more Ctrl-C ends that wait too.
    """
    import sys

    global _quitting
    if _quitting:  # a Ctrl-C landing while it waits: serve's handler, calling it again
        os._exit(130)
    _quitting = True
    try:
        left = remote_writes_running()
        if left:
            _remote_say(
                f"Remote quit with {', '.join(left)} unfinished: "
                "`aisquare fleet ls` shows where the agent is"
            )
        pushes = sys.modules.get("aisquare.services.remote_push")  # loaded by all that push
        if pushes is not None:
            pushes.push_drain(REMOTE_QUIT_PUSH_SECONDS)
    finally:
        os._exit(130)


def remote_wait_for_writes() -> None:
    """Once the fleet UI is gone: see out the writes phones started that still run, saying
    so, and quit at once on Ctrl-C (:func:`_remote_quit_now`).

    Quitting stopped the server and returned after 5 s whatever it was doing, and then
    the process waited, silently, for any write still running: a restart or a switch
    takes up to 40 s, and cut short between its ``/exit`` and its spawn it leaves the
    agent down, so the wait is right and only its silence was not. Then the stopped
    server gets :data:`REMOTE_WINDING_DOWN_SECONDS` to send the answers and stop.
    """
    try:
        if _remote_writes_announced("Ctrl-C"):
            while remote_writes_running():
                time.sleep(0.05)
        with _lock:
            stopped, _winding_down[:] = list(_winding_down), []
        deadline = time.monotonic() + REMOTE_WINDING_DOWN_SECONDS
        for server in stopped:
            server.wound_down(max(0.0, deadline - time.monotonic()))
    except KeyboardInterrupt:
        _remote_quit_now()


def _remote_serve_server(config: uvicorn.Config) -> uvicorn.Server:
    """uvicorn for ``serve``, whose Ctrl-C says what it waits for, and whose second one quits.

    uvicorn's shutdown waits for every request in flight, and a phone's restart or
    switch takes up to 40 s; its log level hides the line saying so. And a second
    Ctrl-C did not end the wait: on Python 3.12 and later its ``wait_closed`` waits for
    the connection all the same, and ``asyncio.run`` then waits for the write's thread.
    With no write running, the second Ctrl-C is uvicorn's own, and the way out still
    clears the deadline and saves ``last_seen``. ``timeout_graceful_shutdown`` would not
    do: it ends the request before its answer goes out, and the thread runs on.
    """
    import signal

    import uvicorn

    class RemoteServe(uvicorn.Server):
        def handle_exit(self, sig: int, frame: FrameType | None) -> None:
            again = self.should_exit and sig == signal.SIGINT
            super().handle_exit(sig, frame)
            if again and remote_writes_running():
                _remote_quit_now()
            _remote_writes_announced("Ctrl-C again")

    return RemoteServe(config)


_lock = threading.Lock()
_runtime: Runtime | None = None
_server: _Server | None = None
_foreground: uvicorn.Server | None = None
"""The server :func:`run_foreground` runs (``asq remote serve``), while it runs."""
_flusher: threading.Timer | None = None
_home_claim: tuple[Path, int] | None = None
"""``remote-serve.lock`` and its descriptor, while this process serves Remote from that home."""
_claiming = threading.Lock()
"""Held while this process claims a home (:func:`_claim_remote_home`, under :data:`_lock`), or
looks whether another process serves one (:func:`remote_served_elsewhere`, never under it):
one at a time. On NFS, Linux makes ``flock`` a lock of the whole process, so a look whose
lock landed as this process claimed the home took that claim for its own, and its unlock let
the home go, for another process's Remote to take as this one served."""

SERVE_LOCK_NAME = "remote-serve.lock"
"""Beside ``remote.json``: held by the one process that serves Remote from that home."""
CLAIM_PATIENCE_SECONDS = 0.05
"""How long a claim of the home keeps trying a lock it finds held: one that asks whether
another process serves (:func:`remote_served_elsewhere`) holds it for a moment, and must never
make a real claim fail. A Remote that is on holds it for as long as it serves."""
REMOTE_ALREADY_ON = (
    "another Remote is on for this ~/.aisquare (the fleet UI's R panel, or `aisquare remote "
    "serve` in another shell) — turn it off first: two would share one link, one passphrase, "
    "one auto-off and one list of phones"
)
REMOTE_WINDING_DOWN = (
    "the Remote turned off last is still answering what phones asked before it went off (a "
    "read waits as long as tmux takes to answer); turn it on again in a moment"
)
"""Why a start waits for the server turned off last, while no phone's write is running
(:func:`_remote_winding_down`)."""


def _remote_winding_down() -> str:
    """Why a start waits for the server turned off last (:class:`RemoteWindingDown`): the
    phone's writes it is still finishing, by name, or else the requests it still answers.

    It blamed "a phone's restart or switch" whatever held it up, while a phone's read waiting
    on a tmux that answers late holds it as long with no write running at all, and seemed
    to say someone was driving the fleet (sweep 3 of #243)."""
    running = remote_writes_running()
    if running:
        return (
            f"the Remote turned off last is still finishing {', '.join(running)} (a restart or "
            "switch can take 40 s); turn it on again once that is done"
        )
    return REMOTE_WINDING_DOWN


def _claim_remote_home(state: Runtime) -> bool:
    """Hold :data:`SERVE_LOCK_NAME` for as long as this process serves Remote; ``True`` when
    this call took it, :class:`RemoteAlreadyOn` when another process holds it.

    Two Remotes on one home, the TUI's and a ``serve`` on another port as the docs
    once advised, share one ``remote.json`` and every file beside it, and undo each
    other: the TUI's switch revoked the phones that had unlocked against ``serve``
    and cleared the deadline, and ``serve``'s timer, reading none, never armed again,
    so ``serve`` ran on past its printed deadline, publicly tunnelled; each server's
    push sender kept its own record of what it had pushed, so every notification
    came twice (sweep of #243). So the second is refused, whichever it is. The lock
    is the operating system's: it goes with the process however that ends. A home
    where the lock file cannot be made or locked for another reason serves without
    it, and says so in the log.

    Called holding :data:`_lock`, which :func:`start_remote_server` keeps until its
    server is recorded. Claimed before that lock was taken, the home was let go by a
    stop ending on another thread in between, which found nothing serving
    (:func:`_release_remote_home`), and the new server ran unclaimed.
    """
    global _home_claim
    path = state._state_path.with_name(SERVE_LOCK_NAME)
    if _home_claim is not None and _home_claim[0] == path:
        return False  # this process serves from here already
    with _claiming:
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            log.warning("remote: %s could not be opened (%s); serving without it", path, exc)
            return False
        patience = time.monotonic() + CLAIM_PATIENCE_SECONDS
        while True:
            try:
                lock_exclusive(fd)
                break
            except OSError as exc:
                if exc.errno in _LOCK_HELD and time.monotonic() < patience:
                    time.sleep(0.005)
                    continue
                os.close(fd)
                if exc.errno in _LOCK_HELD:
                    raise RemoteAlreadyOn(REMOTE_ALREADY_ON) from None
                log.warning("remote: %s could not be locked (%s); serving without it", path, exc)
                return False
        previous, _home_claim = _home_claim, (path, fd)
    if previous is not None:  # another home's, which this process serves no more
        _release_remote_claim(previous[1])
    return True


def remote_served_elsewhere() -> bool:
    """Whether another process serves Remote from this home: holds :data:`SERVE_LOCK_NAME`.

    For the R panel, which said Remote was off while ``asq remote serve``, or another fleet
    UI, served this home publicly, its devices listed and its write switch flipped from that
    same panel (sweep of #243). ``False`` while this process serves from it, or when there is
    no lock file to tell (none is made here). Takes the lock for a moment when it is free,
    which a claim waits out (:data:`CLAIM_PATIENCE_SECONDS`).

    Never as this process claims a home (:data:`_claiming`), and never under :data:`_lock`,
    which every status read takes: on NFS a lock call can block however non-blocking. So
    :data:`_home_claim` is read without it: a claim, which sets it, holds :data:`_claiming`,
    and a release that lands meanwhile leaves nobody serving, which is what this says then.
    """
    path = remote_state_path().with_name(SERVE_LOCK_NAME)
    with _claiming:
        claim = _home_claim
        if claim is not None and claim[0] == path:
            return False
        try:
            fd = os.open(path, os.O_RDWR)
        except OSError:
            return False
        try:
            lock_exclusive(fd)
        except OSError as exc:
            return exc.errno in _LOCK_HELD
        else:
            with contextlib.suppress(OSError):
                unlock(fd)
            return False
        finally:
            os.close(fd)


def _release_remote_home() -> None:
    """Let :data:`SERVE_LOCK_NAME` go: this process serves Remote no more.

    Unless it does again: a server started while an earlier one was still being stopped,
    on a thread of its own, would serve on unclaimed once that stop came to its end. Nor
    while a server it stopped still finishes a phone's write (:data:`_winding_down`): its
    needs watcher and push sender run until it is done, and a Remote another process
    started in those seconds, finding the home let go, pushed every notification beside
    them. The last of those servers lets it go as it ends
    (:func:`_release_remote_home_once_wound_down`).
    """
    global _home_claim
    with _lock:
        if (_server is not None and _server.running) or _foreground is not None:
            return
        if any(stopped.winding_down for stopped in _winding_down):
            return
        claim, _home_claim = _home_claim, None
    if claim is not None:
        _release_remote_claim(claim[1])


def _release_remote_home_once_wound_down(server: _Server) -> None:
    """Wait for a stopped ``server`` to finish what was asked of it, then let the home go,
    if nothing else of this process serves from it (:func:`_release_remote_home`)."""
    server.wound_down(None)
    _release_remote_home()


def _release_remote_claim(fd: int) -> None:
    with contextlib.suppress(OSError):
        unlock(fd)
    os.close(fd)


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
    request with nothing. A ``--dist`` that is a web project's own directory, not its
    build, is refused as ``install-page`` refuses it (:func:`_page_project_not_build`).
    """
    if dist_dir is not None:
        dist = dist_dir.resolve()
        if not (dist / "index.html").is_file():
            return f"no index.html in {dist}"
        return _page_project_not_build(dist)
    if (remote_dist_dir().resolve() / "index.html").is_file():
        return None
    from aisquare.services import remote_page

    return None if remote_page.bundled_page_present() else NO_PAGE_HINT


_PAGE_PROJECT_FILES = ("package.json", "node_modules")
"""What a web project's own directory holds beside its source ``index.html``, and its built
``dist/`` never does."""


def _page_project_not_build(source: Path) -> str | None:
    """The refusal of ``source`` when it is a web project's own directory, not its build;
    ``None`` otherwise.

    Its ``index.html`` is the source the build starts from, so it passed for a page,
    and the whole project was then copied or served: ``node_modules``, the sources,
    whatever else the project keeps there (sweep 3 of #243).
    """
    held = next((name for name in _PAGE_PROJECT_FILES if (source / name).exists()), None)
    if held is None:
        return None
    return (
        f"{source} holds {held}: it is the project, not its build — point at its dist/ "
        "after npm run build"
    )


def _page_copy_skips(source: Path, directory: str, names: list[str]) -> set[str]:
    """What :func:`install_page` leaves behind of ``names`` in ``directory`` of ``source``,
    what a server would not serve from ``source`` (:func:`_built_page_target`): a hidden
    name, and a link that leads out of ``source`` or to a hidden file there."""
    return {
        name
        for name in names
        if name.startswith(".")
        or (
            (Path(directory) / name).is_symlink()
            and _built_page_target(source, Path(directory) / name) is None
        )
    }


def install_page(source: Path) -> Path:
    """Copy a built ``aisquare-remote`` dist into :func:`remote_dist_dir`, atomically.

    ``source`` must contain ``index.html`` (re-checked here even though the CLI
    command already does, so a direct caller gets the same guard), and be no web
    project's own directory (:func:`_page_project_not_build`). What a server would not
    serve from ``source`` stays behind (:func:`_built_page_file`): its hidden files, as
    the bundled page's do, which is where a project keeps ``.env`` and ``.git``, and a
    link that leads out of it or to a hidden file, whose content the copy would
    otherwise hold under the link's own name. The copy lands in a staging directory
    beside the destination and is swapped in with two renames — same filesystem, so
    each rename is atomic — rather than removing the destination first, so a server
    reading the old page mid-swap never sees a half-written one.
    """
    source = source.resolve()
    if not (source / "index.html").is_file():
        raise NoRemotePage(f"no index.html in {source} — build aisquare-remote first")
    project = _page_project_not_build(source)
    if project is not None:
        raise NoRemotePage(project)
    ensure_home()
    destination = remote_dist_dir()
    staging = destination.with_name(f".{destination.name}.staging-{os.getpid()}")
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(source, staging, ignore=functools.partial(_page_copy_skips, source))
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
    """Serve in the background; idempotent while running. ``allow_write`` is left as persisted.

    :class:`RemoteAlreadyOn` while another process serves Remote from this home
    (:func:`_claim_remote_home`): a ``serve``, or another fleet UI's panel. And
    :class:`RemoteWindingDown` while the server this process stopped last still
    finishes a phone's write or request (:data:`_winding_down`): its needs watcher and
    push sender run until then, and a second server beside them, the home already this
    process's, pushed every new item to the phone twice, each sender with its own record
    of what it had pushed (sweep 2 of #243).
    """
    global _server
    problem = _remote_dependency_error()
    if problem is not None:
        raise RemoteUnavailable(problem)
    page_problem = _page_missing(dist_dir)
    if page_problem is not None:
        raise NoRemotePage(page_problem)
    state = runtime()
    claimed = False
    try:
        with _lock:
            if _server is not None and _server.running:
                return state.connection_info(_server.port)
            if any(stopped.winding_down for stopped in _winding_down):
                raise RemoteWindingDown(_remote_winding_down())
            claimed = _claim_remote_home(state)
            state.remote_coming_on()
            app = build_remote_app(state, dist_dir=dist_dir)
            server = _Server(app, port)
            server.start_serving()
            _server = server
    except BaseException:
        if claimed:
            _release_remote_home()
        raise
    _schedule_flush()
    return state.connection_info(port)


def stop_remote_server() -> None:
    """Stop the background server (no-op when it is not running).

    The server stops first and ``remote.json`` is flushed last, best effort, as the
    flusher's every-30-s write is: a file that will not write is logged, never
    raised. The TUI turns Remote off from a Textual timer (auto-off), where an
    exception ends the whole fleet UI, and stops ngrok only once this returns. A
    server still finishing a phone's write after its 5 s keeps the home claimed until
    it is done, on a thread of its own (:func:`_release_remote_home`).
    """
    global _server, _flusher
    with _lock:
        server, _server = _server, None
        flusher, _flusher = _flusher, None
        if server is not None:
            # Told to stop, and among the servers winding down, before the lock goes: a
            # start meanwhile sees it (start_remote_server), as does a stop of an earlier
            # server ending on another thread (_release_remote_home).
            server.stop_serving(0)
            _winding_down.append(server)
    if flusher is not None:
        flusher.cancel()
    if server is not None:
        server.stop_serving()
        with _lock:
            _winding_down[:] = [stopped for stopped in _winding_down if stopped.winding_down]
            parked = server in _winding_down
        if parked:  # the home stays claimed until it is done (_release_remote_home)
            threading.Thread(
                target=_release_remote_home_once_wound_down,
                args=(server,),
                name="asq-remote-wound-down",
                daemon=True,
            ).start()
    if _runtime is not None:
        try:
            _runtime.flush_last_seen()
        except OSError as exc:  # the server is already down; only last_seen is lost
            log.warning("remote: flushing remote.json as the server stopped failed: %s", exc)
        except Exception:
            log.warning("remote: flushing remote.json as the server stopped failed", exc_info=True)
    _release_remote_home()


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

    From here on until the next start, the gate answers every request as past the
    deadline and no unlock makes a device (:meth:`Runtime.remote_going_off`), so none
    is made after the revoke, nor let in once the deadline is cleared. The farewell
    push goes first, to the devices about to be revoked, since a revoked device's
    subscription is dropped; it is sent from a daemon thread and never waits on the
    network here. The TUI's switch and both auto-offs come here; ``asq remote revoke
    --all`` does not (Remote stays on, phones unlock again).
    """
    from aisquare.services import remote_push

    state = runtime()
    state.remote_going_off()
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


_flush_failing = False
"""Whether the flusher's last write of ``remote.json`` failed (:func:`_remote_flush_seen`)."""


def _remote_flush_seen() -> None:
    """The flusher's one write of ``last_seen``, its failure told once a streak.

    ``asq remote serve`` has no log handler, so a warning is the last-resort
    handler's lines on its terminal, under the link and the passphrase: a
    ``remote.json`` that could not be written was a traceback every 30 s, for
    as long as it lasted (review of #243, sweep of round 4). The first failure
    of a streak is a warning, one line for the file system's refusal and a
    traceback for anything else; the rest are debug lines, and the first write
    that works again says so.
    """
    global _flush_failing
    if _runtime is None:
        return
    try:
        _runtime.flush_last_seen()
    except Exception as exc:  # one failed write must not end the flushing for good
        if _flush_failing:
            log.debug("remote: flushing remote.json failed again", exc_info=True)
        elif isinstance(exc, OSError):
            log.warning("remote: flushing remote.json failed: %s", exc)
        else:
            log.warning("remote: flushing remote.json failed", exc_info=True)
        _flush_failing = True
        return
    if _flush_failing:
        _flush_failing = False
        log.info("remote: flushing remote.json works again")


def _schedule_flush() -> None:
    """Persist ``last_seen`` and prune expired devices every 30 s while serving.

    For the TUI's server and for ``asq remote serve`` alike: the flusher used to
    re-arm only while the TUI's ran, so ``serve`` never wrote ``last_seen`` at all.
    """
    global _flusher

    def flush_and_rearm() -> None:
        with _lock:
            serving = (_server is not None and _server.running) or _foreground is not None
        _remote_flush_seen()
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
        turn_off: Callable[[], str | None],
        *,
        timer: Callable[[float, Callable[[], None]], Any] = threading.Timer,
    ) -> None:
        self._state = state
        self._turn_off = turn_off
        self._timer_factory = timer
        self._timer: Any = None
        self._lock = threading.Lock()
        self._cancelled = False
        self.fired = False
        """Whether the deadline passed and Remote was turned off."""
        self.failure: str | None = None
        """What turning off could not do, as ``turn_off`` said it, for the way out to say
        (:meth:`auto_off_outcome`)."""
        self._settled = threading.Event()
        """Set once ``turn_off`` has returned and :attr:`failure` holds what it said."""

    def auto_off_arm(self) -> None:
        """Wait toward the deadline ``remote.json`` holds now; none at all is never."""
        deadline = self._state.auto_off_deadline()
        if deadline is None:
            return
        delay = min(max(0.0, (deadline - _remote_now()).total_seconds()), AUTO_OFF_CHECK_SECONDS)
        with self._lock:
            if self._cancelled:  # a check already running as serve's way out cancelled it
                return
            if self._timer is not None:
                self._timer.cancel()
            self._timer = self._timer_factory(delay, self.auto_off_fire)
            self._timer.daemon = True
            self._timer.start()

    def auto_off_fire(self) -> None:
        """At a check: Remote off when the deadline has passed, once whoever finds it so.

        Both the timer's thread and ``serve``'s way out fire it (:func:`run_foreground`),
        and a cancel cannot stop a check already running: a Ctrl-C as the 30 s check
        found the deadline past would turn Remote off twice, two farewells and two
        revokes."""
        deadline = self._state.auto_off_deadline()
        if deadline is None:
            return
        if deadline > _remote_now():
            self.auto_off_arm()  # not yet, or extended from a phone meanwhile
            return
        with self._lock:
            if self.fired:
                return
            self.fired = True
        try:
            self.failure = self._turn_off()
        finally:
            self._settled.set()

    def auto_off_outcome(self) -> str | None:
        """What turning off could not do, once it is done; ``None`` when it did all of it,
        or never fired.

        Waits for the timer's thread when it fired: ``turn_off`` tells the server to stop
        before it returns what it could not do, and the way out, read as soon as the
        server stopped, found no failure yet when that thread had not run on, and ``serve``
        said "Remote turned off" and exited 0 with the phones still signed in (review of
        #243, round 4).
        """
        if self.fired:
            self._settled.wait()
        return self.failure

    def auto_off_cancel(self) -> None:
        """No more checks: none armed, and none armed again by one already running."""
        with self._lock:
            self._cancelled = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None


def _remote_serve_off(state: Runtime, server: Any) -> str | None:
    """``serve``'s auto-off firing: the farewell, every device revoked (4410), the deadline
    cleared, and the server told to stop, even when ``remote.json`` cannot be written.
    What could not be done comes back as a sentence, for the way out to say
    (:func:`run_foreground`); ``None`` when all of it was.

    It raised instead, on the timer's thread: a traceback, the deadline left in place
    since its clearing never ran, and then "Remote turned off" and exit 0, under
    ``--json`` too, while the phones kept cookies the next Remote accepted (sweep 2 of
    #243). The deadline is cleared on its own try, as the R panel does.

    Its way out waits for a phone's write still running, and says so: told to stop
    already, the server takes the next Ctrl-C as the second, which quits at once
    (:func:`_remote_serve_server`).
    """
    failure: str | None = None
    try:
        try:
            revoke_every_remote_device("auto-off")
        except Exception as exc:
            log.warning("remote: auto-off could not revoke the devices: %s", exc)
            failure = (
                f"its devices could not be revoked ({exc}), so their cookies would open the "
                "next Remote: run `aisquare remote revoke --all` once ~/.aisquare/remote.json "
                "can be written"
            )
        try:
            state.set_auto_off(None)
        except Exception as exc:
            log.warning("remote: auto-off could not clear its deadline: %s", exc)
            failure = failure or f"its deadline is still in ~/.aisquare/remote.json ({exc})"
    finally:
        server.should_exit = True
        _remote_writes_announced("Ctrl-C")
    return failure


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

    ``True`` when auto-off ended it, and :class:`RemoteOffIncomplete` once the server
    is down when auto-off could not revoke the devices or clear the deadline
    (:func:`_remote_serve_off`). In order: this home is claimed
    (:class:`RemoteAlreadyOn` while another process serves Remote from it,
    :func:`_claim_remote_home`) and the port bound (:class:`RemoteBindError` when
    another process holds it), both before ``ready`` prints anything; the deadline is
    set ``auto_off_minutes`` from now (0 is never) and ``public_url`` noted as the
    origin of push links; ``ready`` runs (the CLI's banner); uvicorn serves on the
    bound socket. A timer that reads the wall clock every 30 s turns Remote off at the
    deadline, or at the first check after the machine slept past it, and waits
    on while a phone keeps extending it, with the farewell push and every device
    revoked (4410); the flusher writes ``last_seen`` and prunes devices every
    30 s. Ctrl-C revokes nothing (SPEC §2.4): the devices' own expiry bounds them;
    one that comes once the deadline has passed is auto-off, which does.
    """
    global _foreground, _flusher
    problem = _remote_dependency_error()
    if problem is not None:
        raise RemoteUnavailable(problem)
    page_problem = _page_missing(dist_dir)
    if page_problem is not None:
        raise NoRemotePage(page_problem)
    origin = None if public_url is None else check_public_origin(public_url)
    state = runtime()
    with _lock:
        claimed = _claim_remote_home(state)  # before anything is bound or printed
    state.remote_coming_on()
    try:
        sock = _bind_remote_socket(port)
    except BaseException:
        if claimed:
            _release_remote_home()
        raise
    try:
        app = build_remote_app(state, dist_dir=dist_dir)
        server = _remote_serve_server(_remote_uvicorn_config(app, port))

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
            if minutes and not timer.fired and state.auto_off_passed(_remote_now()):
                # Its time came before its check did, which counts the monotonic clock: a
                # machine that slept past it. A Ctrl-C then is auto-off, which signs every
                # phone out, as it would have half a minute later, not a Ctrl-C, which
                # clears the deadline and signs none out (review of #243, round 5).
                timer.auto_off_fire()
            with _lock:
                _foreground = None
                flusher, _flusher = _flusher, None
            if flusher is not None:
                flusher.cancel()
            try:
                if minutes and not timer.fired:
                    state.set_auto_off(None)  # no server, no deadline: nothing stays on to end
                state.flush_last_seen()
            except OSError as exc:  # the way out reports what ended the server, not this
                log.warning("remote: writing remote.json on the way out failed: %s", exc)
            except Exception:
                log.warning("remote: writing remote.json on the way out failed", exc_info=True)
        failure = timer.auto_off_outcome()
        if failure is not None:
            raise RemoteOffIncomplete(
                f"Remote turned off — the auto-off timer ran out, but {failure}"
            )
        return timer.fired
    finally:
        sock.close()
        if claimed:
            _release_remote_home()


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
    "RemoteAlreadyOn",
    "RemoteBindError",
    "RemoteError",
    "RemoteInfo",
    "RemoteOffIncomplete",
    "RemoteUnavailable",
    "RemoteWindingDown",
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
    "remote_install_hint",
    "remote_server_status",
    "remote_state_loaded",
    "remote_wait_for_writes",
    "remote_writes_running",
    "revoke_remote_device",
    "run_foreground",
    "runtime",
    "set_allow_write",
    "set_auto_off",
    "start_remote_server",
    "stop_remote_server",
]
