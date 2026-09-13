"""The cliXR server: static client on ``/``, one websocket on ``/ws``.

Shape, and why it is this one:

- **One token, not two.** Auth is ``mcp_server.serve_token()`` — the same
  credential ``aisquare serve --show-token`` prints, from the same 0600 file.
  A second token would be a second thing to rotate and a second thing to leak.
  It is compared with :func:`secrets.compare_digest` and it arrives in the
  first websocket frame rather than a header, because a browser cannot set
  headers on a ``WebSocket`` and the alternative is a query string, which ends
  up in logs.
- **Polling, not subscribing.** :mod:`projector` reads the store every
  :data:`POLL_MS`. Nothing in the hook path changes, nothing new has to be
  kept consistent, and the cost is a bounded read twice a second.
- **Fail-open, per connection.** Every connection owns its own store handle,
  its own poll task, its own transcript tail and its own audio buffer. A
  client that vanishes mid-frame, or sends nonsense, or asks for a session
  that is not there, affects nothing else — and no agent session is ever
  blocked on any of it. That is non-negotiable #6.

What this module does NOT do: it never writes to the board except through the
two public service entry points a prompt reaches (``fleet.tell``,
``team.add_note``), it touches no hook, no task lifecycle call, and no
settings file.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import socket
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from importlib.resources import as_file, files
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from aisquare.models import ProjectInfo, TeamSession
from aisquare.services.xr import projector
from aisquare.services.xr.protocol import (
    PROTOCOL_VERSION,
    Ack,
    Audio,
    AudioEnd,
    Auth,
    Error,
    Hello,
    Prompt,
    Session,
    Stt,
    Subscribe,
    Transcript,
    parse_client,
    to_wire,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from starlette.applications import Starlette
    from starlette.websockets import WebSocket

POLL_MS = 500
"""Board poll interval. ``AISQUARE_XR_POLL_MS`` overrides it (the suite sets 20)."""

AUTH_TIMEOUT_S = 5.0
"""How long a socket may stay silent before it has authenticated."""

CLOSE_AUTH_FAILED = 4401
"""Application close code for a failed auth. 4000-4999 is the private range."""

TRANSCRIPT_BACKLOG_BYTES = 8192
"""How much of a transcript to replay on subscribe: the last screen or so."""

MAX_AUDIO_BYTES = 8 * 1024 * 1024
"""Cap on one push-to-talk burst. ~4 minutes of 16 kHz mono PCM."""

TRANSCRIBE: Any = None
"""Hook: ``(bytes) -> str``, set by the speech backend when it lands.

``None`` — the default, and the state of this tree — makes ``audioEnd`` answer
with ``stt_unavailable`` rather than failing in a way the client has to guess
at. A later task assigns ``services.xr.speech.transcribe`` here; nothing in
this module imports it, so the ``[xr]`` extra's whisper dependency stays
unimported until something actually speaks.
"""


def poll_interval() -> float:
    """Seconds between board polls, honouring ``AISQUARE_XR_POLL_MS``.

    Read per call rather than at import so a test can set it after this module
    is loaded — which, given the CLI imports lazily, is every test.
    """
    raw = os.environ.get("AISQUARE_XR_POLL_MS", "").strip()
    try:
        millis = int(raw) if raw else POLL_MS
    except ValueError:
        millis = POLL_MS
    return max(millis, 1) / 1000.0


# --- static assets --------------------------------------------------------------


def web_root() -> Any:
    """The shipped client directory, as an importlib Traversable.

    ``files("aisquare")`` and not ``Path(__file__).parent.parent``: the wheel
    is what an operator installs, this package may be zipped, and the path
    arithmetic version of this line is the one that works in a checkout and
    404s everywhere else.
    """
    return files("aisquare") / "web" / "xr"


@contextlib.contextmanager
def _resolved(relative: str) -> Iterator[Path | None]:
    """A real filesystem path for one asset, or ``None`` if it is not one.

    ``as_file`` because package data is not guaranteed to be on disk; in a
    plain install it is, and this is a no-op. Anything that escapes the web
    root — ``..`` in a URL, an absolute path — resolves outside it and comes
    back ``None``, which the route turns into a 404.
    """
    candidate = web_root()
    for part in Path(relative).parts:
        if part in ("", ".", "..", os.sep) or os.path.isabs(part):
            yield None
            return
        candidate = candidate / part
    try:
        with as_file(candidate) as path:
            yield path if path.is_file() else None
    except (FileNotFoundError, OSError):
        yield None


def build_app(project: ProjectInfo, *, token: str) -> Starlette:
    """The ASGI app for one project's board.

    The token is taken as an argument rather than read here so a test can drive
    a wrong one, and so the value is resolved once at startup instead of per
    frame.
    """
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse, Response
    from starlette.routing import Route, WebSocketRoute

    async def index(request: Request) -> Response:
        return await asset(request)

    async def asset(request: Request) -> Response:
        relative = request.path_params.get("path") or "index.html"
        with _resolved(str(relative)) as path:
            if path is None:
                return PlainTextResponse("not found", status_code=404)
            # Read into memory: `as_file` may have extracted a temporary copy
            # that is unlinked when this block exits, and FileResponse streams
            # after the handler returns.
            return Response(
                path.read_bytes(),
                media_type=_media_type(path.name),
                headers={"cache-control": "no-cache"},
            )

    async def socket(websocket: WebSocket) -> None:
        await _Connection(websocket, project=project, token=token).run()

    return Starlette(
        routes=[
            Route("/", index),
            WebSocketRoute("/ws", socket),
            Route("/{path:path}", asset),
        ]
    )


_MEDIA_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
}


def _media_type(name: str) -> str:
    """Content type by extension.

    A small explicit table rather than :mod:`mimetypes`, whose answer for
    ``.js`` depends on the machine's ``/etc/mime.types`` — and a ``.js`` served
    as ``text/plain`` is a module a browser refuses to execute, which presents
    as a blank page with one console line.
    """
    return _MEDIA_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


# --- one websocket --------------------------------------------------------------


class _Connection:
    """One client socket, from the auth frame to the close.

    State lives here and nowhere else, which is the whole fail-open story: two
    headsets on the same board share the SQLite file and nothing else, and
    neither can wedge the other.
    """

    def __init__(self, websocket: WebSocket, *, project: ProjectInfo, token: str) -> None:
        self._ws = websocket
        self._project = project
        self._token = token
        self._sent: list[Session] = []
        self._unread_since: dict[str, int] = {}
        self._subscribed: str | None = None
        self._tail: asyncio.Task[None] | None = None
        self._audio: bytearray | None = None
        self._transcript_seq = 0

    # -- lifecycle

    async def run(self) -> None:
        """Accept, authenticate, then serve until the client goes away."""
        await self._ws.accept()
        if not await self._authenticate():
            return
        with _store() as store:
            self._seed_watermarks(store)
            await self._send(
                Hello(
                    protocol=PROTOCOL_VERSION,
                    hub=self._project.id,
                    serverTime=datetime.now(tz=UTC).isoformat(),
                )
            )
            snapshot = projector.snapshot(
                store, self._project.id, unread_since=self._unread_since
            )
            self._sent = list(snapshot.sessions)
            await self._send(snapshot)
        poller = asyncio.create_task(self._poll())
        try:
            await self._read_loop()
        finally:
            poller.cancel()
            await self._stop_tail()
            with contextlib.suppress(asyncio.CancelledError):
                await poller

    async def _authenticate(self) -> bool:
        """First frame, within :data:`AUTH_TIMEOUT_S`, or the socket closes.

        Every failure is the same answer — an ``auth_failed`` error frame and
        close ``4401`` — whether the token was absent, wrong, minted against a
        different ``AISQUARE_HOME``, or the frame was not an ``auth`` at all.
        Distinguishing them for the caller would only help someone guessing.
        """
        try:
            raw = await asyncio.wait_for(self._ws.receive_text(), timeout=AUTH_TIMEOUT_S)
            message = parse_client(raw)
        except (TimeoutError, ValidationError, ValueError, KeyError):
            await self._reject()
            return False
        except Exception:  # pragma: no cover - client vanished mid-handshake
            return False
        if not isinstance(message, Auth) or not secrets.compare_digest(
            message.token, self._token
        ):
            await self._reject()
            return False
        return True

    async def _reject(self) -> None:
        with contextlib.suppress(Exception):
            await self._send(
                Error(code="auth_failed", message="the first frame must be a valid auth token")
            )
            await self._ws.close(code=CLOSE_AUTH_FAILED)

    def _seed_watermarks(self, store: Any) -> None:
        """Start every session's unread count at this connection's own arrival.

        A client that has just connected has read nothing and missed nothing:
        counting from the board's current position means a badge only ever
        reflects what happened while this operator was wearing the headset.
        """
        latest = store.latest_seq(self._project.id)
        for row in store.team_sessions(self._project.id):
            self._unread_since[row.id] = latest

    # -- outbound

    async def _send(self, message: Any) -> None:
        await self._ws.send_text(to_wire(message))

    async def _poll(self) -> None:
        """Diff the board on a timer and send what changed.

        Errors are swallowed on purpose: a store that is momentarily locked, or
        a client that closed between the read and the write, must not take the
        connection down with a traceback — the next tick simply tries again.
        """
        interval = poll_interval()
        while True:
            await asyncio.sleep(interval)
            try:
                with _store() as store:
                    current = projector.sessions(
                        store, self._project.id, unread_since=self._unread_since
                    )
                change = projector.delta(self._sent, current)
                self._sent = current
                if change is not None:
                    await self._send(change)
            except asyncio.CancelledError:
                raise
            except Exception:
                return

    # -- inbound

    async def _read_loop(self) -> None:
        """Dispatch client frames until the socket closes.

        A frame that does not parse gets an ``error`` and the socket stays
        open: one malformed message from a client mid-development should not
        cost the operator their ring.
        """
        from starlette.websockets import WebSocketDisconnect

        while True:
            try:
                packet = await self._ws.receive()
            except (WebSocketDisconnect, RuntimeError):
                return
            if packet.get("type") == "websocket.disconnect":
                return
            data = packet.get("bytes")
            if data is not None:
                self._collect_audio(data)
                continue
            text = packet.get("text")
            if text is None:
                continue
            try:
                message = parse_client(text)
            except (ValidationError, ValueError) as exc:
                await self._send(Error(code="bad_message", message=_one_line(exc)))
                continue
            try:
                await self._dispatch(message)
            except Exception as exc:  # a bad request must not kill the socket
                await self._send(Error(code="internal", message=_one_line(exc)))

    async def _dispatch(self, message: Any) -> None:
        if isinstance(message, Subscribe):
            await self._subscribe(message.session)
        elif isinstance(message, Prompt):
            await self._prompt(message)
        elif isinstance(message, Audio):
            self._audio = bytearray()
        elif isinstance(message, AudioEnd):
            await self._transcribe(message.session)
        elif isinstance(message, Auth):
            # Already authenticated; a re-auth is a no-op rather than an error,
            # so a reconnecting client that replays its opening frames is fine.
            return

    # -- transcripts

    async def _subscribe(self, session_id: str | None) -> None:
        """Follow one session's transcript, or stop following.

        Subscribing is also what marks a session read: the watermark moves to
        the board's current position, so the panel's unread badge clears the
        moment the operator focuses it, and counts again from there.
        """
        await self._stop_tail()
        self._subscribed = session_id
        if session_id is None:
            return
        with _store() as store:
            row = store.get_session(session_id)
            self._unread_since[session_id] = store.latest_seq(self._project.id)
        if row is None or row.project_id != self._project.id:
            await self._send(
                Error(code="no_such_session", message=f"no session {session_id} on this board")
            )
            return
        if not row.transcript_path:
            await self._send(
                Error(
                    code="no_transcript",
                    message=f"session {session_id} has no transcript on record",
                )
            )
            return
        self._transcript_seq = 0
        self._tail = asyncio.create_task(self._tail_transcript(row))

    async def _stop_tail(self) -> None:
        if self._tail is None:
            return
        self._tail.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._tail
        self._tail = None

    async def _tail_transcript(self, row: TeamSession) -> None:
        """Replay the tail of a transcript, then follow it as it grows.

        Backlog first (:data:`TRANSCRIPT_BACKLOG_BYTES` from the end, whole
        records only), then a poll on the same interval as the board. Polling
        rather than inotify because the file may be on any filesystem and this
        is a text stream a human reads, not a frame budget.
        """
        path = Path(row.transcript_path or "")
        interval = poll_interval()
        offset = 0
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                start = max(0, size - TRANSCRIPT_BACKLOG_BYTES)
                handle.seek(start)
                backlog = handle.read()
                offset = size
        except OSError as exc:
            await self._send(Error(code="no_transcript", message=_one_line(exc)))
            return
        lines = backlog.split(b"\n")
        if start > 0 and lines:
            lines = lines[1:]  # the first line is a fragment of a clipped record
        for line in lines:
            await self._emit_record(row.id, line)
        while True:
            await asyncio.sleep(interval)
            try:
                with path.open("rb") as handle:
                    handle.seek(offset)
                    fresh = handle.read()
                    offset = handle.tell()
            except OSError:
                return
            if not fresh:
                continue
            for line in fresh.split(b"\n"):
                await self._emit_record(row.id, line)

    async def _emit_record(self, session_id: str, line: bytes) -> None:
        text = _record_text(line)
        if not text:
            return
        self._transcript_seq += 1
        await self._send(
            Transcript(session=session_id, seq=self._transcript_seq, text=text, final=True)
        )

    # -- prompts

    async def _prompt(self, message: Prompt) -> None:
        """Route the operator's text to one session, and say how it landed.

        Two paths, and the client is told which. A session with a live fleet
        pane goes through :func:`services.fleet.tell`, which types into a
        WAITING pane and otherwise files a board note — its own answer is
        reported verbatim in ``ack.detail``. A session with no pane (an MCP
        client, an agent someone started by hand) has nothing to type into, so
        the note is filed directly, addressed to its label or role, where the
        agent's next delta injection delivers it.
        """
        text = message.text.strip()
        if not text:
            await self._send(
                Ack(session=message.session, ok=False, detail="refusing to send an empty prompt")
            )
            return
        with _store() as store:
            row = store.get_session(message.session)
            agent = next(
                (
                    candidate
                    for candidate in store.fleet_agents(self._project.id, live_only=True)
                    if candidate.session_id == message.session
                ),
                None,
            )
        if row is None or row.project_id != self._project.id:
            await self._send(
                Ack(session=message.session, ok=False, detail="no such session on this board")
            )
            return
        ok, detail = await asyncio.to_thread(
            _deliver, self._project, row, agent.label if agent is not None else None, text
        )
        await self._send(Ack(session=message.session, ok=ok, detail=detail))

    # -- audio

    def _collect_audio(self, chunk: bytes) -> None:
        """Buffer one binary frame, if a burst is open.

        Bytes outside an ``audio``/``audioEnd`` pair are dropped rather than
        buffered: a client that sends audio without a header has told the
        server nothing about which session it is for.
        """
        if self._audio is None:
            return
        if len(self._audio) + len(chunk) > MAX_AUDIO_BYTES:
            self._audio = None
            return
        self._audio.extend(chunk)

    async def _transcribe(self, session_id: str) -> None:
        """End the burst: hand the buffer to :data:`TRANSCRIBE`, or say why not.

        The result goes back as an ``stt`` frame and stops there. Turning a
        final transcription into a ``prompt`` for ``session_id`` is the client's
        call — the operator gets to see what was heard before it is sent — and
        the server half of that round trip belongs to the M6 task, not this one.
        """
        buffered, self._audio = self._audio, None
        hook = TRANSCRIBE
        if hook is None:
            await self._send(
                Error(
                    code="stt_unavailable",
                    message="speech-to-text is not wired up in this build",
                )
            )
            return
        if not buffered:
            await self._send(
                Error(
                    code="stt_empty",
                    message=f"no audio arrived for the burst addressed to {session_id}",
                )
            )
            return
        try:
            text = await asyncio.to_thread(hook, bytes(buffered))
        except Exception as exc:
            await self._send(Error(code="stt_failed", message=_one_line(exc)))
            return
        await self._send(Stt(text=text or "", final=True))


# --- helpers --------------------------------------------------------------------


def _deliver(project: ProjectInfo, row: TeamSession, label: str | None, text: str) -> tuple[
    bool, str
]:
    """Blocking half of :meth:`_Connection._prompt`. Runs off the event loop."""
    from aisquare.services import fleet as fleet_service
    from aisquare.services import team as team_service

    if label is not None:
        try:
            result = fleet_service.tell(project, label, text)
        except Exception as exc:
            return False, _one_line(exc)
        return result.delivered, result.how
    to_role = row.label or row.role
    try:
        event = team_service.add_note(text, to_role=to_role, cwd=project.root)
    except Exception as exc:
        return False, _one_line(exc)
    return True, f"filed as board note #{event.seq} to {to_role}"


def _record_text(line: bytes) -> str:
    """The human-readable text of one Claude Code transcript record.

    User and assistant text blocks only. Tool calls, results, thinking blocks
    and the envelope's metadata are all skipped: what the focus panel renders
    is the conversation, and everything else is either noise at this size or
    content that was never meant to be projected onto a wall.
    """
    stripped = line.strip()
    if not stripped:
        return ""
    try:
        record = json.loads(stripped)
    except (ValueError, UnicodeDecodeError):
        return ""
    if not isinstance(record, dict) or record.get("type") not in ("user", "assistant"):
        return ""
    message = record.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(
            block.get("text"), str
        )
    ]
    return "\n".join(part for part in parts if part.strip()).strip()


def _one_line(exc: object) -> str:
    """An exception as one line of message text, never a traceback."""
    return " ".join(str(exc).split())[:500]


@contextlib.contextmanager
def _store() -> Iterator[Any]:
    """A store for the length of one read, imported late.

    Late because ``core.store`` opens nothing at import time but does pull in
    the whole model layer, and this module is imported by the CLI's dependency
    guard before anyone has decided to serve anything.
    """
    from aisquare.core.store import store_session

    with store_session() as store:
        yield store


# --- process ---------------------------------------------------------------------


def port_in_use(bind: str, port: int) -> bool:
    """Whether something already holds ``bind:port``.

    Checked before uvicorn starts so the failure is the CLI's error contract
    within two seconds rather than a traceback out of asyncio several lines
    later. A racing bind between this check and uvicorn's is possible and
    unimportant: the second one still fails, just less prettily.
    """
    family = socket.AF_INET6 if ":" in bind else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((bind, port))
        except OSError:
            return True
    return False


def run(project: ProjectInfo, *, bind: str, port: int, token: str) -> None:
    """Serve until interrupted."""
    import uvicorn

    uvicorn.run(build_app(project, token=token), host=bind, port=port, log_level="warning")


__all__: Sequence[str] = (
    "POLL_MS",
    "TRANSCRIBE",
    "build_app",
    "port_in_use",
    "run",
    "web_root",
)
