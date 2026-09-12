"""The Remote Control server: one local port that shows the fleet to a phone.

``asq remote serve`` runs it in the foreground; the fleet UI's Remote modal runs
it in a background thread through :func:`start` / :func:`stop`. Either way it
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
``~/.aisquare/remote.json`` at 0600, and a serving process re-reads it when its
bytes change — ``aisquare remote allow-write on`` from another shell reaches the
running server within a second (:meth:`Runtime.reload_if_changed`). Everything
that touches real systems goes through :class:`Sources` and :class:`Writes`, two
bags of callables the tests replace — the server itself never opens the store or
spawns tmux.

Dependencies: starlette and uvicorn (already here through the ``serve`` extra) and
``websockets`` (uvicorn's WebSocket backend) — the ``remote`` extra in pyproject.
All three are imported lazily so this module, and the modal that imports it,
load in a base install; :func:`start` and the CLI say what to install.
"""

from __future__ import annotations

import asyncio
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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from aisquare.core.paths import (
    ensure_home,
    remote_audit_path,
    remote_dist_dir,
    remote_state_path,
)
from aisquare.core.version import __version__
from aisquare.models import FleetAgent, FleetAgentStatus, ProjectInfo, TeamSession, TurnMetric

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.websockets import WebSocket

    from aisquare.core.tmux import Capture

log = logging.getLogger(__name__)

BIND = "127.0.0.1"
DEFAULT_PORT = 8748
COOKIE = "asq_remote"
TICK_SECONDS = 1.0
UNLOCK_LIMIT = 5
UNLOCK_WINDOW_SECONDS = 60.0
WS_CLOSE_UNAUTHORIZED = 4401
"""Close code the page keys on: after it the adapter routes to /unlock."""

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
"""Shown by the modal's status line, ``asq remote serve``'s exit, and ``start()``'s raise —
one sentence, so a fresh machine never sees a server that quietly answers with nothing."""

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


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp() -> str:
    return _now().isoformat(timespec="seconds")


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


# --- state --------------------------------------------------------------------------


@dataclass
class Device:
    """One unlocked browser: the cookie session and what it told us about itself."""

    sid: str
    ua: str
    first_seen: str
    last_seen: str

    def as_json(self) -> dict[str, str]:
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

    def as_json(self) -> dict[str, object]:
        return {
            "token": self.token,
            "password": self.password,
            "allow_write": self.allow_write,
            "auto_off_at": self.auto_off_at,
            "sessions": [device.as_json() for device in self.sessions],
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


class Runtime:
    """The server's mutable state: ``remote.json``, the live sockets, the audit log.

    Shared between the uvicorn thread and whoever called :func:`start` (the
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
        self._state = self._load()

    # -- persistence --

    @staticmethod
    def _digest(data: bytes) -> bytes:
        return hashlib.blake2b(data, digest_size=16).digest()

    def _signature(self) -> tuple[bytes, bytes] | None:
        """``(digest, bytes)`` of the file right now, or ``None`` when it cannot be read."""
        try:
            data = self._state_path.read_bytes()
        except OSError:
            return None
        return (self._digest(data), data)

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

    def _load(self) -> _State:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = None
        state = (
            _State.from_json(raw) if isinstance(raw, dict) else _State(new_token(), new_password())
        )
        self._write(state)
        return state

    def _write(self, state: _State) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_name(self._state_path.name + ".tmp")
        encoded = json.dumps(state.as_json(), indent=2).encode("utf-8")
        tmp.write_bytes(encoded)
        tmp.chmod(0o600)
        tmp.replace(self._state_path)
        self._state_path.chmod(0o600)
        # Our own write, by content: the next check finds these exact bytes and
        # skips the parse; a sibling process writing the same size in the same
        # mtime tick is still seen, because its bytes differ.
        self._disk = self._digest(encoded)

    def _save(self) -> None:
        with self._lock:
            self._write(self._state)

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

    def info(self, port: int = DEFAULT_PORT) -> RemoteInfo:
        with self._lock:
            return RemoteInfo(self.token, self.password, build_local_url(self.token, port))

    def token_matches(self, supplied: str) -> bool:
        return _same(supplied, self.token)

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
            self._save()

    def set_auto_off(self, at: datetime | None) -> None:
        with self._lock:
            self.reload_if_changed()
            self._state.auto_off_at = at.isoformat(timespec="seconds") if at else None
            self._save()

    def regenerate_password(self) -> str:
        """A new password; every unlocked device is dropped with the old one."""
        with self._lock:
            self.reload_if_changed()
            self._state.password = new_password()
            for device in list(self._state.sessions):
                self._drop(device.sid)
            self._save()
            return self._state.password

    # -- sessions --

    def unlock(self, password: str, ua: str) -> str | None:
        """A new session id when ``password`` is right, else ``None``."""
        with self._lock:
            self.reload_if_changed()
            if not _same(password, self._state.password):
                return None
            sid = new_token()
            stamp = _stamp()
            self._state.sessions.append(Device(sid, ua[:200], stamp, stamp))
            self._save()
            return sid

    def session(self, sid: str | None) -> Device | None:
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
            return [device.as_json() for device in self._state.sessions]

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

    def revoke(self, sid: str) -> bool:
        """Drop the cookie session and close its websockets; ``True`` if it existed."""
        with self._lock:
            self.reload_if_changed()
            dropped = self._drop(sid)
            if dropped:
                self._save()
            return dropped

    def flush(self) -> None:
        """Persist ``last_seen`` (called on a timer, not per request).

        Another process's change lands first: a flush that wrote memory over a
        fresher file would undo the very ``allow-write on`` this is about.
        """
        with self._lock:
            self.reload_if_changed()
            self._save()

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
        """``ts sid endpoint summary`` — one line per write that went through (§4-E)."""
        line = f"{_stamp()} {sid} {endpoint} {summary}\n"
        with self._lock:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self._audit_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
            with contextlib.suppress(OSError):
                self._audit_path.chmod(0o600)


# --- what the server reads and writes: the seams -----------------------------------


Snapshot = Callable[[], object]
FleetSource = Callable[[str | None], object]
"""An optional project (id/name/codename) → that project's ``fleet ls --json`` payload.
``None`` is the CURRENT project — byte-identical to today. Raises :class:`NoSuchProject`."""
PaneSource = Callable[[str, str | None, int], dict[str, object]]
"""Agent label, optional project, scrollback lines → one pane capture.

``history`` of 0 is today's live-screen-only frame, byte for byte (§4-L).
Raises :class:`NoSuchAgent` / :class:`NoSuchProject`."""
TranscriptSource = Callable[[str, str | None, int, str | None], dict[str, object]]
"""Agent, optional project, limit, ``before`` cursor → one page of conversation (§4-M).

A missing or unreadable transcript is an EMPTY page, never an error: an agent
that has not written one yet must still open in the page."""
ExplainabilitySource = Callable[[str], dict[str, object]]
"""Agent label → the §4-I card payload. Raises :class:`NoSuchAgent` only; never anything else."""
WriteHandler = Callable[[dict[str, Any]], tuple[dict[str, object], str]]
"""Body in → ``(result, audit summary)``; raise :class:`RequestError` to refuse."""


@dataclass(frozen=True)
class Sources:
    """The read-only JSON — by default the very functions ``asq --json`` prints."""

    projects: Snapshot
    fleet: FleetSource
    board: Snapshot
    tasks: Snapshot
    memory: Snapshot
    panes: PaneSource
    transcript: TranscriptSource = field(
        default=lambda label, project, limit, before: _live_transcript(
            label, project, limit, before
        )
    )
    explainability: ExplainabilitySource = field(default=lambda label: _live_explainability(label))


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
    label: str, project: str | None = None, limit: int = 0, before: str | None = None
) -> dict[str, object]:
    """One page of the agent's own conversation, from the board's transcript (§4-M).

    The pane cannot answer this: agent panes are alternate-screen and tmux keeps
    no scrollback for them. The board already records where the transcript is.
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
        width=_pane_width(agent),
    )
    return page.as_json()


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


def live_sources() -> Sources:
    """The real thing: the ``--json`` builders over the live store and tmux."""

    def projects() -> object:
        from aisquare.cli.common import projects_json
        from aisquare.services import fleet as fleet_service
        from aisquare.services import project as project_service

        all_projects = project_service.list_projects()
        rows = projects_json(all_projects)
        for row, one in zip(rows, all_projects, strict=True):
            agents = fleet_service.list_agents(one, live_only=True)
            row["agents"] = _agent_state_counts(agents)
        return rows

    def fleet(project: str | None = None) -> object:
        from aisquare.cli.fleet import agents_json
        from aisquare.services import fleet as fleet_service

        target = _resolve_project(project)
        return agents_json(target, fleet_service.list_agents(target, live_only=True))

    def board() -> object:
        from aisquare.cli.team import board_json
        from aisquare.services import team as team_service

        return board_json(*team_service.board_data())

    def tasks() -> object:
        from aisquare.services import team as team_service

        return [task.model_dump(mode="json") for task in team_service.list_tasks(None)]

    def memory() -> object:
        from aisquare.services import context as context_service

        return [entry.model_dump(mode="json") for entry in context_service.list_entries()]

    return Sources(
        projects, fleet, board, tasks, memory, _live_panes, _live_transcript, _live_explainability
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


def _live_explainability(label: str) -> dict[str, object]:
    """The card for one live agent; only an unknown label raises (→ 404)."""
    from aisquare.core.store import store_session
    from aisquare.services import fleet as fleet_service
    from aisquare.services import metrics as metrics_service

    project = fleet_service.resolve_project(None)
    with store_session() as store:
        agent = store.fleet_agent_by_label(project.id, label, live_only=True)
        if agent is None:
            raise NoSuchAgent(f"no live agent {label!r} in {project.root.name or project.id}")
        session = store.get_session(agent.session_id) if agent.session_id else None
    turns: list[TurnMetric] = []
    if agent.session_id:
        try:
            turns = metrics_service.recent(project_id=project.id, session_id=agent.session_id)
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


def _optional(body: dict[str, Any], key: str) -> str | None:
    """An optional NAME or reference — blank and whitespace-only both mean absent.

    Correct for a project ref, a note's task or a role. WRONG for literal text a
    human typed: see :func:`_literal`.
    """
    value = body.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _literal(body: dict[str, Any], key: str) -> str | None:
    """Text to deliver verbatim — whitespace is CONTENT here, not emptiness.

    ``_optional`` answers "did they name something", and a name that is all
    spaces is no name. A keystroke that is all spaces is a keystroke. Reading
    typed text with ``_optional`` is what silently ate the space bar: a flush of
    ``" "`` became ``None``, so a write carrying only a space delivered nothing
    while the endpoint answered 200 ``sent: true``, and the audit line recorded
    ``text=0ch`` — the trail honestly reporting that no text was sent, the loss
    having happened before it. Absent or non-string is still absent; ``""`` is
    still nothing to send.
    """
    value = body.get(key)
    return value if isinstance(value, str) else None


def live_writes() -> Writes:
    """The write endpoints over the services the CLI commands call."""

    def task_claim(body: dict[str, Any]) -> tuple[dict[str, object], str]:
        from aisquare.services import team as team_service

        task = team_service.claim_task(_required(body, "ref"), session_ref=_optional(body, "as"))
        return {"task": task.model_dump(mode="json")}, f"claimed {task.id}"

    def task_done(body: dict[str, Any]) -> tuple[dict[str, object], str]:
        from aisquare.services import team as team_service

        task = team_service.finish_task(
            _required(body, "ref"), note=_optional(body, "note"), session_ref=_optional(body, "as")
        )
        return {"task": task.model_dump(mode="json")}, f"done {task.id}"

    def note(body: dict[str, Any]) -> tuple[dict[str, object], str]:
        from aisquare.services import team as team_service

        event = team_service.add_note(
            _required(body, "text"),
            session_ref=_optional(body, "as"),
            task_ref=_optional(body, "task"),
            to_role=_optional(body, "to"),
            kind=_optional(body, "kind") or "note",
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

    def send_keys(body: dict[str, Any]) -> tuple[dict[str, object], str]:
        from aisquare.core.store import store_session
        from aisquare.services import fleet as fleet_service

        label = _required(body, "agent")
        text = _literal(body, "text")
        keys = body.get("keys")
        if keys is not None and not (
            isinstance(keys, list) and all(isinstance(key, str) for key in keys)
        ):
            raise RequestError(400, "invalid", "'keys' must be a list of tmux key names")
        enter = bool(body.get("enter", False))
        if not text and not keys and not enter:
            raise RequestError(400, "invalid", "give 'text', 'keys' or 'enter'")
        target = _resolve_project(_optional(body, "project"))
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
            "note": note,
            "project/switch": project_switch,
            "project/add": project_add,
            "project/remove": project_remove,
            "send-keys": send_keys,
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

    def get(self, kind: str, compute: Snapshot) -> object:
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


def _json_error(status: int, error: str, message: str | None = None) -> Response:
    from starlette.responses import JSONResponse

    body: dict[str, object] = {"error": error}
    if message:
        body["message"] = message
    return JSONResponse(body, status_code=status)


class _TokenGate:
    """Pure ASGI: anything not under ``/r/<the token>/`` is a 404, HTTP and WS alike."""

    def __init__(self, app: Any, runtime: Runtime) -> None:
        self._app = app
        self._runtime = runtime

    def _accepted(self, path: str) -> bool:
        if not path.startswith("/r/"):
            return False
        rest = path[3:]
        supplied = rest.split("/", 1)[0]
        return bool(supplied) and self._runtime.token_matches(supplied)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        kind = scope.get("type")
        if kind not in ("http", "websocket") or self._accepted(scope.get("path", "")):
            await self._app(scope, receive, send)
            return
        if kind == "http":
            await _json_error(404, "not_found")(scope, receive, send)
            return
        if "websocket.http.response" in scope.get("extensions", {}):
            await send(
                {
                    "type": "websocket.http.response.start",
                    "status": 404,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "websocket.http.response.body", "body": b'{"error": "not_found"}'})
            return
        await send({"type": "websocket.close", "code": 4404})


def build_app(
    runtime: Runtime,
    *,
    sources: Sources | None = None,
    writes: Writes | None = None,
    dist_dir: Path | None = None,
    tick: float = TICK_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> _TokenGate:
    """The ASGI app. Everything real is behind ``sources``/``writes``; tests pass fakes."""
    try:
        from starlette.applications import Starlette
        from starlette.responses import FileResponse, JSONResponse
        from starlette.routing import Mount, Route, WebSocketRoute
        from starlette.websockets import WebSocketDisconnect
    except ImportError as exc:  # pragma: no cover - exercised only in a base install
        raise RemoteUnavailable(f"the remote extra is not installed — {INSTALL_HINT}") from exc

    reads = sources or live_sources()
    handlers = (writes or live_writes()).handlers
    dist = (dist_dir or remote_dist_dir()).resolve()
    limiter = _RateLimiter(clock)
    cache = _Cache(ttl=tick * 0.9)

    def cookie_path(request: Request) -> str:
        return f"/r/{request.path_params['token']}"

    def device_of(request: Request) -> Device | None:
        return runtime.session(request.cookies.get(COOKIE))

    async def snapshot(kind: str, compute: Snapshot) -> object:
        return await asyncio.to_thread(cache.get, kind, compute)

    def guarded(compute: Snapshot, kind: str) -> Callable[[Request], Any]:
        async def endpoint(request: Request) -> Response:
            if device_of(request) is None:
                return _json_error(401, "unauthorized")
            try:
                payload = await snapshot(kind, compute)
            except LookupError as exc:
                return _json_error(404, "not_found", str(exc))
            except Exception as exc:
                log.warning("remote: %s snapshot failed: %s", kind, exc)
                return _json_error(503, "unavailable", str(exc))
            return JSONResponse(payload)

        return endpoint

    async def unlock(request: Request) -> Response:
        if not limiter.allow(_client_of(request.scope)):
            return _json_error(429, "too_many_attempts", "5 attempts a minute — wait")
        try:
            body = await request.json()
        except ValueError:
            body = None
        password = body.get("password") if isinstance(body, dict) else None
        if not isinstance(password, str):
            return _json_error(400, "invalid", 'send {"password": "..."}')
        sid = runtime.unlock(password, request.headers.get("user-agent", ""))
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
        if device_of(request) is None:
            return _json_error(401, "unauthorized")
        return JSONResponse(runtime.remote_json())

    async def devices_endpoint(request: Request) -> Response:
        device = device_of(request)
        if device is None:
            return _json_error(401, "unauthorized")
        rows: list[dict[str, object]] = [
            {**row, "current": row["sid"] == device.sid} for row in runtime.device_rows()
        ]
        return JSONResponse(rows)

    async def revoke_device(request: Request) -> Response:
        device = device_of(request)
        if device is None:
            return _json_error(401, "unauthorized")
        sid = request.path_params["sid"]
        if not runtime.revoke(sid):
            return _json_error(404, "not_found", "no such device")
        runtime.audit(device.sid, "devices/revoke", sid)
        return JSONResponse({"ok": True, "sid": sid})

    async def fleet_endpoint(request: Request) -> Response:
        if device_of(request) is None:
            return _json_error(401, "unauthorized")
        project = request.query_params.get("project") or None
        try:
            payload = await asyncio.to_thread(
                cache.get, f"fleet:{project or ''}", lambda: reads.fleet(project)
            )
        except LookupError as exc:
            return _json_error(404, "not_found", str(exc))
        except Exception as exc:
            log.warning("remote: fleet snapshot failed: %s", exc)
            return _json_error(503, "unavailable", str(exc))
        return JSONResponse(payload)

    async def panes(request: Request) -> Response:
        if device_of(request) is None:
            return _json_error(401, "unauthorized")
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
        if device_of(request) is None:
            return _json_error(401, "unauthorized")
        agent = request.path_params["agent"]
        project = request.query_params.get("project") or None
        before = request.query_params.get("before") or None
        try:
            limit = _limit_param(request.query_params.get("limit"))
        except ValueError as exc:
            return _json_error(400, "invalid", str(exc))
        try:
            payload = await asyncio.to_thread(reads.transcript, agent, project, limit, before)
        except LookupError as exc:
            return _json_error(404, "not_found", str(exc))
        except Exception as exc:
            log.warning("remote: transcript for %s failed: %s", agent, exc)
            return _json_error(503, "unavailable", str(exc))
        return JSONResponse(payload)

    async def explainability(request: Request) -> Response:
        if device_of(request) is None:
            return _json_error(401, "unauthorized")
        agent = request.path_params["agent"]
        try:
            payload = await asyncio.to_thread(reads.explainability, agent)
        except LookupError as exc:
            return _json_error(404, "not_found", str(exc))
        except Exception as exc:  # §4-I: never raises, never blocks the other endpoints
            log.warning("remote: explainability for %s failed: %s", agent, exc)
            payload = {"available": False, "reason": f"explainability lookup failed: {exc}"}
        return JSONResponse(payload)

    async def write(request: Request) -> Response:
        device = device_of(request)
        if device is None:
            return _json_error(401, "unauthorized")
        name = request.path_params["name"]
        handler = handlers.get(name) if name in WRITE_ENDPOINTS else None
        if handler is None:
            return _json_error(404, "not_found")
        if not runtime.allow_write:
            return _json_error(403, "read_only", READ_ONLY_REASON)
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            return _json_error(400, "invalid", "the body must be a JSON object")
        try:
            result, summary = await asyncio.to_thread(handler, body)
        except RequestError as exc:
            return _json_error(exc.status, exc.error, exc.message)
        except LookupError as exc:
            return _json_error(404, "not_found", str(exc))
        except Exception as exc:
            log.warning("remote: write %s failed: %s", name, exc)
            return _json_error(400, "write_failed", str(exc))
        runtime.audit(device.sid, name, summary)
        return JSONResponse(result)

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
        device = runtime.session(websocket.cookies.get(COOKIE))
        if device is None:
            if "websocket.http.response" in websocket.scope.get("extensions", {}):
                await websocket.send_denial_response(_json_error(401, "unauthorized"))
            else:
                await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
            return
        await websocket.accept()
        sid = device.sid
        loop = asyncio.get_running_loop()
        subscribed: set[str] = set()
        fleet_project: str | None = None
        """``None`` = the CURRENT project (today's behaviour, unchanged); set by
        a ``{subscribe_fleet: "<project>"}`` text frame to receive that project's
        fleet frames instead — one active target per socket, like ``subscribe``
        for panes. §4-D: the frame shape is unchanged, ``{type:"fleet", payload,
        ts}``; only WHICH project's ``fleet ls`` payload fills it moves."""
        last: dict[str, str] = {}

        async def close_unauthorized() -> None:
            with contextlib.suppress(Exception):
                await websocket.close(code=WS_CLOSE_UNAUTHORIZED)

        def closer() -> None:
            loop.call_soon_threadsafe(lambda: loop.create_task(close_unauthorized()))

        async def send_frame(kind: str, payload: object, agent: str | None = None) -> None:
            frame: dict[str, object] = {"type": kind, "payload": payload, "ts": _stamp()}
            if agent is not None:
                frame["agent"] = agent
            await websocket.send_text(json.dumps(frame))

        async def push_if_changed(key: str, kind: str, payload: object, agent: str | None) -> None:
            encoded = json.dumps(payload, sort_keys=True)
            if last.get(key) != encoded:
                last[key] = encoded
                await send_frame(kind, payload, agent)

        async def tick_once() -> None:
            try:
                payload = await snapshot("board", reads.board)
                await push_if_changed("board", "board", payload, None)
            except Exception as exc:
                log.debug("remote: board frame skipped: %s", exc)
            fleet_key = f"fleet:{fleet_project or ''}"
            try:
                fleet_payload = await asyncio.to_thread(
                    cache.get, fleet_key, lambda: reads.fleet(fleet_project)
                )
                await push_if_changed(fleet_key, "fleet", fleet_payload, None)
            except Exception as exc:
                log.debug("remote: fleet frame skipped: %s", exc)
            await push_if_changed("remote", "remote", runtime.remote_json(), None)
            for agent in sorted(subscribed):
                try:
                    # §4-L: history is a FETCH, live stays a stream — 0 keeps
                    # this frame exactly the §4-D shape it has always had.
                    payload = await asyncio.to_thread(reads.panes, agent, None, 0)
                except Exception as exc:
                    payload = {"rows": [], "width": 0, "height": 0, "error": str(exc)}
                await push_if_changed(f"pane:{agent}", "pane", payload, agent)

        async def reader() -> None:
            while True:
                text = await websocket.receive_text()
                try:
                    message = json.loads(text)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                target = message.get("subscribe")
                if isinstance(target, str) and target:
                    subscribed.add(target)
                    last.pop(f"pane:{target}", None)
                target = message.get("unsubscribe")
                if isinstance(target, str):
                    subscribed.discard(target)
                target = message.get("subscribe_fleet")
                if isinstance(target, str):
                    nonlocal fleet_project
                    fleet_project = target or None
                    last.pop(f"fleet:{fleet_project or ''}", None)

        runtime.register_socket(sid, closer)
        reading = asyncio.ensure_future(reader())
        try:
            while not reading.done():
                if runtime.session(sid) is None:
                    await close_unauthorized()
                    break
                await tick_once()
                await asyncio.wait([reading], timeout=tick)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            log.debug("remote: stream for %s ended: %s", sid, exc)
        finally:
            runtime.unregister_socket(sid, closer)
            reading.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reading

    api_routes = [
        Route("/api/unlock", unlock, methods=["POST"]),
        Route("/api/remote", remote, methods=["GET"]),
        Route("/api/projects", guarded(reads.projects, "projects"), methods=["GET"]),
        Route("/api/fleet", fleet_endpoint, methods=["GET"]),
        Route("/api/board", guarded(reads.board, "board"), methods=["GET"]),
        Route("/api/tasks", guarded(reads.tasks, "tasks"), methods=["GET"]),
        Route("/api/memory", guarded(reads.memory, "memory"), methods=["GET"]),
        Route("/api/devices", devices_endpoint, methods=["GET"]),
        Route("/api/devices/{sid}", revoke_device, methods=["DELETE"]),
        Route("/api/panes/{agent}", panes, methods=["GET"]),
        Route("/api/transcript/{agent}", transcript, methods=["GET"]),
        Route("/api/explainability/{agent}", explainability, methods=["GET"]),
        Route("/api/{name:path}", write, methods=["POST"]),
        Route("/api/{rest:path}", api_missing),
        WebSocketRoute("/ws", stream),
        Route("/", static, methods=["GET"]),
        Route("/{path:path}", static, methods=["GET"]),
    ]
    inner = Starlette(routes=[Mount("/r/{token}", routes=api_routes)])
    return _TokenGate(inner, runtime)


# --- process lifecycle: the module API the TUI modal calls (PLAN §4-F) ----------------


def _dependency_error() -> str | None:
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
        self._thread = threading.Thread(target=self._run, name="asq-remote", daemon=True)

    def _run(self) -> None:
        # uvicorn answers a failed bind with sys.exit(3); in a thread that is
        # noise, and start() already turns "never started" into a RemoteError.
        with contextlib.suppress(SystemExit):
            self._server.run()

    def start(self, timeout: float = 5.0) -> None:
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

    def stop(self, timeout: float = 5.0) -> None:
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

    Checked up front by :func:`start` and :func:`run_foreground`, not by
    :func:`build_app` itself: an explicit ``--dist``/``dist_dir`` that turns out
    to be wrong is still a per-request 404 (``test_missing_dist_is_a_404_...``),
    because the caller named that path on purpose and may still be building it.
    What must never happen silently is the DEFAULT — ``dist_dir=None`` falling
    back to :func:`remote_dist_dir`, which nothing populates until
    :func:`install_page` runs — so a fresh machine's first ``m`` press gets a
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


def start(dist_dir: Path | None = None, port: int = DEFAULT_PORT) -> RemoteInfo:
    """Serve in the background; idempotent while running. ``allow_write`` is left as persisted."""
    global _server
    problem = _dependency_error()
    if problem is not None:
        raise RemoteUnavailable(problem)
    page_problem = _page_missing(dist_dir)
    if page_problem is not None:
        raise NoRemotePage(page_problem)
    state = runtime()
    with _lock:
        if _server is not None and _server.running:
            return state.info(_server.port)
        app = build_app(state, dist_dir=dist_dir)
        server = _Server(app, port)
        server.start()
        _server = server
    _schedule_flush()
    return state.info(port)


def stop() -> None:
    """Stop the background server (no-op when it is not running)."""
    global _server, _flusher
    with _lock:
        server, _server = _server, None
        flusher, _flusher = _flusher, None
    if flusher is not None:
        flusher.cancel()
    if server is not None:
        server.stop()
    if _runtime is not None:
        _runtime.flush()


def status() -> dict[str, object]:
    """``{running, sessions:[{sid, ua, first_seen, last_seen}]}`` (PLAN §4-F)."""
    with _lock:
        running = _server is not None and _server.running
    return {"running": running, "sessions": runtime().device_rows()}


def revoke(sid: str) -> bool:
    """Drop a device's cookie session and close its websockets."""
    return runtime().revoke(sid)


def set_allow_write(enabled: bool) -> None:
    """Flip the write gate — the ONLY way it turns on; the default is off."""
    runtime().set_allow_write(enabled)


def regenerate_password() -> str:
    """A fresh password; every unlocked device has to unlock again."""
    return runtime().regenerate_password()


def set_auto_off(at: datetime | None) -> None:
    """Record when the modal will switch Remote off (shown as ``auto_off_at``)."""
    runtime().set_auto_off(at)


def _schedule_flush() -> None:
    """Persist ``last_seen`` every 30 s while serving, instead of once per request."""
    global _flusher

    def run() -> None:
        with _lock:
            serving = _server is not None and _server.running
        if _runtime is not None:
            _runtime.flush()
        if serving:
            _schedule_flush()

    with _lock:
        if _flusher is not None:
            _flusher.cancel()
        _flusher = threading.Timer(30.0, run)
        _flusher.daemon = True
        _flusher.start()


def run_foreground(dist_dir: Path | None = None, port: int = DEFAULT_PORT) -> None:
    """``asq remote serve``: block in this thread until Ctrl-C."""
    problem = _dependency_error()
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
    "explainability_payload",
    "install_page",
    "live_sources",
    "live_writes",
    "regenerate_password",
    "revoke",
    "run_foreground",
    "runtime",
    "set_allow_write",
    "set_auto_off",
    "start",
    "status",
    "stop",
]
