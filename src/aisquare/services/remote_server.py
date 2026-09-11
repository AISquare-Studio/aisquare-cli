"""The Remote Control server: one local port that shows the fleet to a phone.

``asq remote serve`` runs it in the foreground; the fleet UI's Remote modal runs
it in a background thread through :func:`start` / :func:`stop`. Either way it
binds ``127.0.0.1`` only — ngrok (or the same machine's browser) is the only way
in — and every path lives under ``/r/<token>/``:

* ``/r/<token>/``                 the built ``aisquare-remote`` page (SPA fallback)
* ``POST /r/<token>/api/unlock``  ``{password}`` → ``Set-Cookie: asq_remote=<sid>``
* ``GET  /r/<token>/api/...``     ``projects fleet board tasks memory panes/<agent>
                                  devices remote`` — the read-only JSON
* ``WS   /r/<token>/ws``          frames ``{type, agent?, payload, ts}`` every second
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
``~/.aisquare/remote.json`` at 0600. Everything that touches real systems goes
through :class:`Sources` and :class:`Writes`, two bags of callables the tests
replace — the server itself never opens the store or spawns tmux.

Dependencies: starlette and uvicorn (already here through the ``serve`` extra) and
``websockets`` (uvicorn's WebSocket backend) — the ``remote`` extra in pyproject.
All three are imported lazily so this module, and the modal that imports it,
load in a base install; :func:`start` and the CLI say what to install.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aisquare.core.paths import (
    ensure_home,
    remote_audit_path,
    remote_dist_dir,
    remote_state_path,
)
from aisquare.core.version import __version__

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.websockets import WebSocket

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

INSTALL_HINT = "pip install 'aisquare-cli[remote]' (or: pipx inject aisquare-cli websockets)"

_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
"""Typed on a phone, read off a terminal: no 0/O, 1/l/I."""


class RemoteError(RuntimeError):
    """The server could not do what was asked (start, stop, revoke)."""


class RemoteUnavailable(RemoteError):
    """The ``remote`` extra is not installed."""


class RequestError(Exception):
    """A handler's refusal, carried to the client as ``{error, message}``."""

    def __init__(self, status: int, error: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message


class NoSuchAgent(LookupError):
    """``panes/<agent>`` or ``send-keys`` named an agent the project does not have."""


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp() -> str:
    return _now().isoformat(timespec="seconds")


def new_token() -> str:
    """The 32-character path token — 24 random bytes, URL-safe."""
    import secrets  # kept off the hook path (tests/test_iam_single_reader.py)

    return secrets.token_urlsafe(24)


def new_password() -> str:
    """An 8-character password from the unambiguous alphabet."""
    import secrets

    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(8))


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
        self._state = self._load()
        self._closers: dict[str, set[Callable[[], None]]] = {}

    # -- persistence --

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
        tmp.write_text(json.dumps(state.as_json(), indent=2), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(self._state_path)
        self._state_path.chmod(0o600)

    def _save(self) -> None:
        with self._lock:
            self._write(self._state)

    # -- identity --

    @property
    def token(self) -> str:
        return self._state.token

    @property
    def password(self) -> str:
        return self._state.password

    @property
    def allow_write(self) -> bool:
        return self._state.allow_write

    def info(self, port: int = DEFAULT_PORT) -> RemoteInfo:
        with self._lock:
            return RemoteInfo(self.token, self.password, build_local_url(self.token, port))

    def token_matches(self, supplied: str) -> bool:
        return _same(supplied, self.token)

    def remote_json(self) -> dict[str, object]:
        """``GET /api/remote`` — the ONE place remote-specific state is exposed (§4-B)."""
        with self._lock:
            return {
                "allow_write": self._state.allow_write,
                "auto_off_at": self._state.auto_off_at,
                "version": __version__,
            }

    # -- switches --

    def set_allow_write(self, enabled: bool) -> None:
        with self._lock:
            self._state.allow_write = bool(enabled)
            self._save()

    def set_auto_off(self, at: datetime | None) -> None:
        with self._lock:
            self._state.auto_off_at = at.isoformat(timespec="seconds") if at else None
            self._save()

    def regenerate_password(self) -> str:
        """A new password; every unlocked device is dropped with the old one."""
        with self._lock:
            self._state.password = new_password()
            for device in list(self._state.sessions):
                self._drop(device.sid)
            self._save()
            return self._state.password

    # -- sessions --

    def unlock(self, password: str, ua: str) -> str | None:
        """A new session id when ``password`` is right, else ``None``."""
        with self._lock:
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
        with self._lock:
            for device in self._state.sessions:
                if _same(sid, device.sid):
                    device.last_seen = _stamp()
                    return device
        return None

    def devices(self) -> list[dict[str, str]]:
        with self._lock:
            return [device.as_json() for device in self._state.sessions]

    def _drop(self, sid: str) -> bool:
        before = len(self._state.sessions)
        self._state.sessions = [d for d in self._state.sessions if d.sid != sid]
        for close in self._closers.pop(sid, set()):
            try:
                close()
            except Exception:  # a socket already gone must not stop the revoke
                log.debug("remote: closing a websocket on revoke failed", exc_info=True)
        return len(self._state.sessions) != before

    def revoke(self, sid: str) -> bool:
        """Drop the cookie session and close its websockets; ``True`` if it existed."""
        with self._lock:
            dropped = self._drop(sid)
            if dropped:
                self._save()
            return dropped

    def flush(self) -> None:
        """Persist ``last_seen`` (called on a timer, not per request)."""
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
PaneSource = Callable[[str], dict[str, object]]
WriteHandler = Callable[[dict[str, Any]], tuple[dict[str, object], str]]
"""Body in → ``(result, audit summary)``; raise :class:`RequestError` to refuse."""


@dataclass(frozen=True)
class Sources:
    """The read-only JSON — by default the very functions ``asq --json`` prints."""

    projects: Snapshot
    fleet: Snapshot
    board: Snapshot
    tasks: Snapshot
    memory: Snapshot
    panes: PaneSource


@dataclass(frozen=True)
class Writes:
    """The write endpoints (§4-E) — by default the same services the CLI commands call."""

    handlers: dict[str, WriteHandler]


def _live_panes(label: str) -> dict[str, object]:
    from aisquare.core.store import store_session
    from aisquare.services import fleet as fleet_service

    project = fleet_service.resolve_project(None)
    with store_session() as store:
        agent = store.fleet_agent_by_label(project.id, label, live_only=True)
    if agent is None:
        raise NoSuchAgent(f"no live agent {label!r} in {project.root.name or project.id}")
    capture = fleet_service.server_for(agent.tmux_socket).capture(agent.pane_id)
    return {
        "rows": capture.lines,
        "cursor": [capture.facts.cursor_x, capture.facts.cursor_y],
        "width": capture.facts.width,
        "height": capture.facts.height,
    }


def live_sources() -> Sources:
    """The real thing: the ``--json`` builders over the live store and tmux."""

    def projects() -> object:
        from aisquare.cli.common import projects_json
        from aisquare.services import project as project_service

        return projects_json(project_service.list_projects())

    def fleet() -> object:
        from aisquare.cli.fleet import agents_json
        from aisquare.services import fleet as fleet_service

        project = fleet_service.resolve_project(None)
        return agents_json(project, fleet_service.list_agents(project, live_only=True))

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

    return Sources(projects, fleet, board, tasks, memory, _live_panes)


def _required(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RequestError(400, "invalid", f"{key!r} is required")
    return value.strip()


def _optional(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    return value if isinstance(value, str) and value.strip() else None


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
        text = _optional(body, "text")
        keys = body.get("keys")
        if keys is not None and not (
            isinstance(keys, list) and all(isinstance(key, str) for key in keys)
        ):
            raise RequestError(400, "invalid", "'keys' must be a list of tmux key names")
        enter = bool(body.get("enter", False))
        if text is None and not keys and not enter:
            raise RequestError(400, "invalid", "give 'text', 'keys' or 'enter'")
        project = fleet_service.resolve_project(None)
        with store_session() as store:
            agent = store.fleet_agent_by_label(project.id, label, live_only=True)
        if agent is None:
            raise NoSuchAgent(f"no live agent {label!r}")
        server = fleet_service.server_for(agent.tmux_socket)
        if text:
            server.send_literal(agent.pane_id, text)
        if keys:
            server.send_keys(agent.pane_id, *keys)
        if enter:
            server.send_keys(agent.pane_id, "Enter")
        summary = f"{label} text={len(text or '')}ch keys={len(keys or [])} enter={enter}"
        return {"agent": label, "sent": True}, summary

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
        response.set_cookie(COOKIE, sid, httponly=True, samesite="lax", path=cookie_path(request))
        return response

    async def remote(request: Request) -> Response:
        if device_of(request) is None:
            return _json_error(401, "unauthorized")
        return JSONResponse(runtime.remote_json())

    async def devices(request: Request) -> Response:
        if device_of(request) is None:
            return _json_error(401, "unauthorized")
        return JSONResponse(runtime.devices())

    async def revoke_device(request: Request) -> Response:
        device = device_of(request)
        if device is None:
            return _json_error(401, "unauthorized")
        sid = request.path_params["sid"]
        if not runtime.revoke(sid):
            return _json_error(404, "not_found", "no such device")
        runtime.audit(device.sid, "devices/revoke", sid)
        return JSONResponse({"ok": True, "sid": sid})

    async def panes(request: Request) -> Response:
        if device_of(request) is None:
            return _json_error(401, "unauthorized")
        agent = request.path_params["agent"]
        try:
            payload = await asyncio.to_thread(reads.panes, agent)
        except LookupError as exc:
            return _json_error(404, "not_found", str(exc))
        except Exception as exc:
            log.warning("remote: pane capture for %s failed: %s", agent, exc)
            return _json_error(503, "unavailable", str(exc))
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
                return FileResponse(candidate)
        index = dist / "index.html"
        if index.is_file():
            return FileResponse(index)
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
            for kind, compute in (("board", reads.board), ("fleet", reads.fleet)):
                try:
                    payload = await snapshot(kind, compute)
                except Exception as exc:
                    log.debug("remote: %s frame skipped: %s", kind, exc)
                    continue
                await push_if_changed(kind, kind, payload, None)
            await push_if_changed("remote", "remote", runtime.remote_json(), None)
            for agent in sorted(subscribed):
                try:
                    payload = await asyncio.to_thread(reads.panes, agent)
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
        Route("/api/fleet", guarded(reads.fleet, "fleet"), methods=["GET"]),
        Route("/api/board", guarded(reads.board, "board"), methods=["GET"]),
        Route("/api/tasks", guarded(reads.tasks, "tasks"), methods=["GET"]),
        Route("/api/memory", guarded(reads.memory, "memory"), methods=["GET"]),
        Route("/api/devices", devices, methods=["GET"]),
        Route("/api/devices/{sid}", revoke_device, methods=["DELETE"]),
        Route("/api/panes/{agent}", panes, methods=["GET"]),
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


def start(dist_dir: Path | None = None, port: int = DEFAULT_PORT) -> RemoteInfo:
    """Serve in the background; idempotent while running. ``allow_write`` is left as persisted."""
    global _server
    problem = _dependency_error()
    if problem is not None:
        raise RemoteUnavailable(problem)
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
    return {"running": running, "sessions": runtime().devices()}


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
    import uvicorn

    app = build_app(runtime(), dist_dir=dist_dir)
    uvicorn.run(app, host=BIND, port=port, log_level="warning", ws="auto")


__all__ = [
    "BIND",
    "COOKIE",
    "DEFAULT_PORT",
    "READ_ONLY_REASON",
    "WRITE_ENDPOINTS",
    "WS_CLOSE_UNAUTHORIZED",
    "Device",
    "NoSuchAgent",
    "RemoteError",
    "RemoteInfo",
    "RemoteUnavailable",
    "RequestError",
    "Runtime",
    "Sources",
    "Writes",
    "build_app",
    "build_local_url",
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
