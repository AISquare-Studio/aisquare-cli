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
import logging
import os
import socket
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import as_file, files
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

from pydantic import ValidationError

from aisquare.core.store import AmbiguousIdError
from aisquare.models import ProjectInfo, TeamSession
from aisquare.services.xr import projector
from aisquare.services.xr.protocol import (
    AUDIO_CHANNELS,
    AUDIO_SAMPLE_BITS,
    AUDIO_SAMPLE_RATE_HZ,
    CLOSE_AUTH_FAILED,
    CLOSE_AUTH_TIMEOUT,
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

_log = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from starlette.applications import Starlette
    from starlette.websockets import WebSocket

POLL_MS = 500
"""Board poll interval. ``AISQUARE_XR_POLL_MS`` overrides it (the suite sets 20)."""

AUTH_TIMEOUT_S = 5.0
"""How long a socket may stay silent before it has authenticated."""

TRANSCRIPT_BACKLOG_BYTES = 8192
"""How much of a transcript to replay on subscribe: the last screen or so.

A budget, not a hard cut: the backlog always contains at least the last complete
record even when that record is itself larger than this (a Read/Bash
``tool_result``, a long answer), because cutting at a fixed offset and dropping
the leading fragment replays NOTHING when the final record spans the whole
window. See :func:`_read_backlog`.
"""

TRANSCRIPT_MISSING_GRACE_S = 5.0
"""How long a followed transcript may be absent before the client is told it is gone.

A rename-then-create rotation leaves the path missing for a tick or two; ending
the tail on the first :class:`FileNotFoundError` turns that ordinary gap into a
panel that is dead for the life of the connection with nothing saying why. So a
missing file is retried for this long, and only a loss that outlasts it is
surfaced (``transcript_gone``) rather than swallowed.
"""

MAX_AUDIO_SECONDS = 260.0
"""How many seconds of audio one push-to-talk burst may buffer (~4.3 minutes).

The cap exists to bound memory against a stuck trigger or a client replaying a
file, not to time a spoken command; the resolution is a poll interval.
"""

MAX_AUDIO_BYTES = int(
    AUDIO_SAMPLE_RATE_HZ * (AUDIO_SAMPLE_BITS // 8) * AUDIO_CHANNELS * MAX_AUDIO_SECONDS
)
"""Cap on one burst in bytes, DERIVED from the wire format rather than written out.

``protocol``'s ``AUDIO_*`` constants are the one home of the format
(sample rate, sample width, channels); this is those times
:data:`MAX_AUDIO_SECONDS`, so the cap tracks the format instead of being a
literal that silently means a different duration the day the format changes.
"""

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
        await _Connection(websocket, project=project, token=token).serve_client()

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
        self._board_seq = 0
        self._tail: asyncio.Task[None] | None = None
        self._audio: bytearray | None = None
        self._audio_session: str | None = None
        self._transcript_seq = 0
        self._pending_reset = False

    # -- lifecycle

    async def serve_client(self) -> None:
        """Accept, authenticate, then serve until the client goes away.

        Not ``run`` for the same reason ``_send_frame`` is not ``_send``.
        """
        await self._ws.accept()
        if not await self._authenticate():
            return
        with _store() as store:
            self._seed_watermarks(store)
            await self._send_frame(
                Hello(
                    protocol=PROTOCOL_VERSION,
                    hub=self._project.id,
                    server_time=datetime.now(tz=UTC).isoformat(),
                )
            )
            snapshot = projector.snapshot(
                store,
                self._project.id,
                unread_since=self._unread_since,
                unread_floor=self._board_seq,
            )
            self._sent = list(snapshot.sessions)
            await self._send_frame(snapshot)
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

        Two OUTCOMES, told apart because a client's reconnect policy turns on
        which one it was:

        - A token that was checked and REJECTED — absent, wrong, or minted
          against a different ``AISQUARE_HOME`` — is ``auth_failed`` + close
          :data:`CLOSE_AUTH_FAILED` (4401), ``retry:false``. It will be just as
          wrong next time, so a client that reconnects on it spins forever.
        - A handshake that never got as far as checking a token — no frame
          within the timeout, or a first frame that was not a valid ``auth``
          (not JSON, wrong type, unknown field) — is a distinct error code and
          close :data:`CLOSE_AUTH_TIMEOUT` (4408), ``retry:true``. No credential
          was judged, so the cause is transport (a reverse-forward not up yet, a
          wrong port, a lost first frame) and reconnecting is right.

        The old code answered every one of these with ``auth_failed`` + 4401, so
        a single stalled handshake — a valid token arriving 5.5 s late — told the
        client to give up on a token that was in fact good, and the ring stayed
        dark until the printed URL was reopened. Only a genuine token rejection
        is terminal now.
        """
        try:
            raw = await asyncio.wait_for(self._ws.receive_text(), timeout=AUTH_TIMEOUT_S)
        except TimeoutError:
            await self._reject(
                code="auth_timeout",
                close=CLOSE_AUTH_TIMEOUT,
                message=f"no auth frame arrived within {AUTH_TIMEOUT_S:.0f}s",
            )
            return False
        except Exception:  # pragma: no cover - client vanished mid-handshake
            return False
        try:
            message = parse_client(raw)
        except (ValidationError, ValueError, KeyError):
            await self._reject(
                code="auth_invalid",
                close=CLOSE_AUTH_TIMEOUT,
                message="the first frame was not a valid auth message",
            )
            return False
        # Imported here, not at module scope: `secrets` pulls in hashlib and
        # ssl, and tests/test_iam_single_reader.py ratchets those out of the
        # import graph the hook path walks on every prompt.
        import secrets

        if not isinstance(message, Auth):
            await self._reject(
                code="auth_invalid",
                close=CLOSE_AUTH_TIMEOUT,
                message="the first frame must be an auth message",
            )
            return False
        # Encoded, for the reason `mcp_server._BearerGuard` already documents:
        # str-mode compare_digest raises TypeError on a non-ASCII argument, so
        # a token with one emoji in it would leave this function as an
        # unhandled exception instead of an `auth_failed` frame. UTF-8 both
        # sides keeps the comparison constant-time and total.
        supplied = message.token.encode("utf-8", "surrogatepass")
        if not secrets.compare_digest(supplied, self._token.encode("utf-8", "surrogatepass")):
            await self._reject(
                code="auth_failed",
                close=CLOSE_AUTH_FAILED,
                message="the token was rejected",
            )
            return False
        return True

    async def _reject(self, *, code: str, close: int, message: str) -> None:
        """Answer a failed handshake with one error frame, then close with ``close``.

        The error ``code`` and the close code are set together because they are
        one signal in two forms: an error frame the client can read and a close
        code its transport sees even if the frame is lost.
        """
        with contextlib.suppress(Exception):
            await self._send_frame(Error(code=code, message=message))
            await self._ws.close(code=close)

    def _seed_watermarks(self, store: Any) -> None:
        """Start every session's unread count at this connection's own arrival.

        A client that has just connected has read nothing and missed nothing:
        counting from the board's current position means a badge only ever
        reflects what happened while this operator was wearing the headset.

        :attr:`_board_seq` is captured here as the connection's starting
        position and stays put. It is the floor a LATE JOINER — a session that
        appears after connect and so is never in this map — counts from:
        :func:`projector.sessions` receives it as ``unread_floor`` and defaults
        any un-watermarked session to it. That default is what retired the old
        per-tick ``_seed_late_joiners`` pass, which re-read ``latest_seq`` and
        the whole session table on every poll to write watermarks this floor now
        supplies for free.
        """
        latest = store.latest_seq(self._project.id)
        for row in store.team_sessions(self._project.id):
            self._unread_since[row.id] = latest
        self._board_seq = latest

    # -- outbound

    async def _send_frame(self, message: Any) -> None:
        """Put one server message on the socket.

        Named ``_send_frame`` rather than ``_send`` on purpose:
        ``tests/test_config_writes_stay_in_the_cli.py`` builds its call graph by
        NAME across the whole package, so a generic name here silently merges
        this connection with every other ``_send`` in the tree — including
        ``cli/ui/terminal.py``'s, which reaches a config write through doctor's
        ``--fix``. The guard then reports that every MCP tool can write config.
        It cannot, and neither can this; the collision was the whole finding.
        """
        await self._ws.send_text(to_wire(message))

    async def _poll(self) -> None:
        """Diff the board on a timer and send what changed.

        The two failures here are not the same failure, so they are not handled
        the same way. A READ that fails is transient — SQLite is busy, another
        process holds the write lock — and the next tick simply tries again; a
        connection that gave up on the first locked store would be a ring that
        goes dark under exactly the contention a ten-agent fleet produces. A
        SEND that fails means the client is gone, and there is nothing left to
        poll for: the loop ends and the read side notices in its own time.

        Neither raises. Non-negotiable #6 is that a dead client affects nothing,
        and a traceback out of a background task here is how that stops being
        true.
        """
        interval = poll_interval()
        while True:
            await asyncio.sleep(interval)
            try:
                with _store() as store:
                    current = projector.sessions(
                        store,
                        self._project.id,
                        unread_since=self._unread_since,
                        unread_floor=self._board_seq,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                continue
            change = projector.delta(self._sent, current)
            self._sent = current
            if change is None:
                continue
            try:
                await self._send_frame(change)
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
                await self._collect_audio(data)
                continue
            text = packet.get("text")
            if text is None:
                continue
            try:
                message = parse_client(text)
            except (ValidationError, ValueError) as exc:
                await self._send_frame(Error(code="bad_message", message=_one_line(exc)))
                continue
            try:
                await self._dispatch(message)
            except Exception as exc:  # a bad request must not kill the socket
                await self._send_frame(Error(code="internal", message=_one_line(exc)))

    async def _dispatch(self, message: Any) -> None:
        if isinstance(message, Subscribe):
            await self._subscribe(message.session)
        elif isinstance(message, Prompt):
            await self._prompt(message)
        elif isinstance(message, Audio):
            self._open_burst(message)
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

        Two things this method is careful about, both bugs the old order had:

        - **Resolve before stopping.** The current tail is only cancelled once a
          real session on THIS board is in hand. A subscribe that cannot resolve
          — an ambiguous prefix, or an id that belongs to another board — leaves
          the transcript the operator was reading exactly where it was, instead
          of killing it and then answering with an error.
        - **Resolve on this board only.** :meth:`store.get_session_in_project`
          scopes the prefix lookup to this project, so a prefix that is unique
          here is not answered ``ambiguous_session`` because a session the
          operator cannot see, on another board sharing the store, also starts
          with those characters.
        """
        if session_id is None:
            await self._stop_tail()
            return
        with _store() as store:
            try:
                row = store.get_session_in_project(self._project.id, session_id)
            except AmbiguousIdError as exc:
                # A prefix that matches two sessions is the CLIENT's mistake.
                # Left to escape, it reaches _read_loop's catch-all and the
                # headset is told `internal` — a server fault — for a string it
                # can fix by typing one more character. The tail is untouched.
                await self._send_frame(Error(code="ambiguous_session", message=_one_line(exc)))
                return
            if row is None:
                # Scoped to this project, so this is genuinely absent here — not
                # a session hidden on another board. The tail is untouched.
                await self._send_frame(
                    Error(code="no_such_session", message=f"no session {session_id} on this board")
                )
                return
            # Focusing a panel marks it read, keyed on the id the BOARD uses
            # (row.id). get_session_in_project resolves PREFIXES, and every frame
            # that goes back out carries row.id, so keying this on the client's
            # short string would watermark an id nothing ever counts and freeze
            # the badge for the life of the socket.
            self._unread_since[row.id] = store.latest_seq(self._project.id)
        # A real session on this board: now it is safe to replace the old tail.
        await self._stop_tail()
        if not row.transcript_path:
            await self._send_frame(
                Error(
                    code="no_transcript",
                    message=f"session {session_id} has no transcript on record",
                )
            )
            return
        self._transcript_seq = 0
        self._pending_reset = False
        self._tail = asyncio.create_task(self._tail_transcript(row))

    async def _stop_tail(self) -> None:
        if self._tail is None:
            return
        self._tail.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._tail
        self._tail = None

    async def _tail_transcript(self, row: TeamSession) -> None:
        """:meth:`_stream_transcript`, with a dead client made quiet.

        This runs as a bare task, so an exception escaping it is an
        ``asyncio`` "Task exception was never retrieved" on someone's terminal
        — from a headset being taken off, which is not an event anyone needs
        reported. The read loop notices the same close on its own.
        """
        try:
            await self._stream_transcript(row)
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def _stream_transcript(self, row: TeamSession) -> None:
        """Replay the tail of a transcript, then follow it as it grows and changes.

        Backlog first (:func:`_read_backlog`: about :data:`TRANSCRIPT_BACKLOG_BYTES`
        from the end, whole records only, and always at least the last complete
        record), then a poll on the same interval as the board. Polling rather
        than inotify because the file may be on any filesystem and this is a text
        stream a human reads, not a frame budget.

        Four things each tick guards against, each a way the old single-file,
        grow-only loop went silently wrong:

        - **Only whole lines are consumed.** A JSONL record is one turn —
          kilobytes — and a poll landing mid-write sees a fragment. Advancing
          past it would parse it as nothing and then never see the rest: the
          panel skips a turn. So the offset rewinds to the last newline and the
          fragment is re-read next tick, when it is whole.
        - **The row is re-read every tick** (:meth:`_current_transcript_path`).
          A resumed session whose row is re-pointed at a new file used to be
          followed on the old one forever; now the tail switches files.
        - **Replacement is detected by identity, not only by shrink.** A
          transcript swapped (``os.replace``, a reused ``transcript_path``) for
          one at least as long as the read offset passes the size check and was
          read from a stale offset, skipping its leading records. Comparing
          ``(st_ino, st_dev)`` across ticks catches the swap; a shorter file
          still trips the size check too. Either way the replacement is re-read
          from a bounded backlog with :attr:`Transcript.reset` set, so the client
          clears the panel instead of appending the new file below the old — and
          the replay is capped rather than streaming the whole file through one
          unbounded read.
        - **A briefly-missing file is retried** for
          :data:`TRANSCRIPT_MISSING_GRACE_S` (a rename-then-create rotation),
          and only a loss that outlasts that is surfaced as ``transcript_gone``.
          The old loop returned on the first :class:`OSError`, ending the tail
          permanently with no frame, no error and no close.
        """
        session_id = row.id
        interval = poll_interval()
        first_path = Path(row.transcript_path or "")
        state: _TailState | None = None
        missing_since: float | None = None
        while True:
            target = (
                first_path
                if state is None
                else (self._current_transcript_path(session_id) or state.path)
            )
            try:
                with target.open("rb") as handle:
                    identity = _file_identity(handle)
                    handle.seek(0, os.SEEK_END)
                    size = handle.tell()
                    if state is None:
                        data = _read_backlog(handle, size, TRANSCRIPT_BACKLOG_BYTES)
                        complete, partial = _whole_lines(data)
                        state = _TailState(
                            path=target, offset=size - len(partial), identity=identity
                        )
                        await self._emit_records(session_id, complete, reset=False)
                    elif target != state.path or identity != state.identity or size < state.offset:
                        data = _read_backlog(handle, size, TRANSCRIPT_BACKLOG_BYTES)
                        complete, partial = _whole_lines(data)
                        state = _TailState(
                            path=target, offset=size - len(partial), identity=identity
                        )
                        await self._emit_records(session_id, complete, reset=True)
                    else:
                        handle.seek(state.offset)
                        fresh = handle.read()
                        if fresh:
                            complete, partial = _whole_lines(fresh)
                            state.offset += len(fresh) - len(partial)
                            await self._emit_records(session_id, complete, reset=False)
                missing_since = None
            except FileNotFoundError as exc:
                if state is None:
                    # Nothing was ever read: the row names a transcript not on
                    # disk. Fail fast, like a subscribe to a transcript-less
                    # session, rather than sitting out the grace period.
                    await self._send_frame(Error(code="no_transcript", message=_one_line(exc)))
                    return
                now = time.monotonic()
                if missing_since is None:
                    missing_since = now
                elif now - missing_since >= TRANSCRIPT_MISSING_GRACE_S:
                    await self._send_frame(
                        Error(
                            code="transcript_gone",
                            message=(
                                f"the transcript for {session_id} went away and did not come back"
                            ),
                        )
                    )
                    return
            except OSError as exc:
                if state is None:
                    await self._send_frame(Error(code="no_transcript", message=_one_line(exc)))
                return
            await asyncio.sleep(interval)

    def _current_transcript_path(self, session_id: str) -> Path | None:
        """The row's transcript_path right now, or ``None`` if it cannot be read.

        Re-read every tick so a session re-pointed at a new transcript on an
        ordinary resume is followed to the new file rather than read forever on
        the old one. ``None`` (an unreadable store, a row that lost its path)
        leaves the caller on the file it already has.
        """
        try:
            with _store() as store:
                row = store.get_session(session_id)
        except Exception:
            return None
        if row is None or not row.transcript_path:
            return None
        return Path(row.transcript_path)

    async def _emit_records(self, session_id: str, lines: Sequence[bytes], *, reset: bool) -> None:
        """Emit a run of records; on ``reset`` restart the sequence and flag the first.

        ``reset`` restarts :attr:`_transcript_seq` at 0 and latches a pending
        flag that rides the FIRST frame actually sent (records with no rendered
        text are skipped, so the flag waits for one that is not), telling the
        client the stream restarted before that frame.
        """
        if reset:
            self._transcript_seq = 0
            self._pending_reset = True
        for line in lines:
            await self._emit_record(session_id, line)

    async def _emit_record(self, session_id: str, line: bytes) -> None:
        text = _record_text(line)
        if not text:
            return
        self._transcript_seq += 1
        reset, self._pending_reset = self._pending_reset, False
        await self._send_frame(
            Transcript(
                session=session_id, seq=self._transcript_seq, text=text, final=True, reset=reset
            )
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
            await self._send_frame(
                Ack(session=message.session, ok=False, detail="refusing to send an empty prompt")
            )
            return
        with _store() as store:
            try:
                row = store.get_session_in_project(self._project.id, message.session)
            except AmbiguousIdError as exc:
                # The same fix subscribe got, on the path that shares no code
                # with it: an ambiguous prefix used to escape to _read_loop's
                # catch-all as `internal` (a server fault) while subscribe
                # answered `ambiguous_session`. Report it in the ack rather than
                # filing the prompt against a guessed session.
                await self._send_frame(
                    Ack(session=message.session, ok=False, detail=_one_line(exc))
                )
                return
            if row is None:
                await self._send_frame(
                    Ack(session=message.session, ok=False, detail="no such session on this board")
                )
                return
            # Match the live pane on the RESOLVED id. A prefix never equals the
            # full stored session_id, so matching on the client's raw string
            # found no agent and filed every prefix-addressed prompt as a board
            # note instead of typing it into the waiting pane.
            agent = next(
                (
                    candidate
                    for candidate in store.fleet_agents(self._project.id, live_only=True)
                    if candidate.session_id == row.id
                ),
                None,
            )
        ok, detail = await asyncio.to_thread(
            _deliver, self._project, row, agent.label if agent is not None else None, text
        )
        await self._send_frame(Ack(session=row.id, ok=ok, detail=detail))

    # -- audio

    def _open_burst(self, message: Audio) -> None:
        """Open a push-to-talk burst, and remember whose microphone it is.

        The HEADER's ``session`` owns the burst. It used to be dropped on the
        floor and the burst attributed to whatever ``audioEnd`` carried, so
        ``audio(session=A)`` followed by ``audioEnd(session=B)`` transcribed
        A's microphone into B's panel, silently — and the schema could not warn
        anyone, because it required ``session`` on both frames and documented
        no relationship between them. Now the two must agree.
        """
        self._audio = bytearray()
        self._audio_session = message.session

    async def _collect_audio(self, chunk: bytes) -> None:
        """Buffer one binary frame, or answer once and drop a burst past the cap.

        Bytes outside an ``audio``/``audioEnd`` pair are dropped: a client that
        sends audio without a header has said nothing about which session it is
        for. A burst that runs past :data:`MAX_AUDIO_BYTES` is answered ONCE with
        ``audio_too_long`` and then discarded — later frames of the same burst
        find a closed buffer and are dropped in silence, and the eventual
        ``audioEnd`` is a no-op. The old code dropped the burst with no frame at
        all and then answered ``audioEnd`` with ``stt_empty`` "no audio arrived"
        after megabytes had, telling the operator "no audio" on every retry of a
        burst that was simply too long (or in the wrong, larger format), with the
        cap stated nowhere in the contract.
        """
        if self._audio_session is None:
            return  # no open header: nothing says which session these bytes are for
        if self._audio is None:
            return  # this burst already ran past the cap and was answered once
        if len(self._audio) + len(chunk) > MAX_AUDIO_BYTES:
            self._audio = None
            await self._send_frame(
                Error(
                    code="audio_too_long",
                    message=(
                        f"the burst exceeded {MAX_AUDIO_BYTES} bytes "
                        f"(~{MAX_AUDIO_SECONDS:.0f}s of audio) and was dropped — "
                        "release the trigger and send a shorter one"
                    ),
                )
            )
            return
        self._audio.extend(chunk)

    async def _transcribe(self, session_id: str) -> None:
        """End the burst: hand the buffer to :data:`TRANSCRIBE`, or say why not.

        The HEADER owns the burst. An ``audioEnd`` naming a different session
        than the ``audio`` header that opened it does NOT re-address the speech:
        the microphone was opened for the header's session and the samples were
        recorded for it, so the disagreement is logged and the header wins.
        Refusing instead (the old ``session_mismatch``) discarded the operator's
        sentence over a client bookkeeping bug — and a header addressed by prefix
        with an ``audioEnd`` carrying the full id, the exact ids the server's own
        frames use, disagree as raw strings and so were refused every time.

        The result goes back as an ``stt`` frame and stops there. Turning a final
        transcription into a ``prompt`` is the client's call — the operator sees
        what was heard before it is sent — and the server half of that round trip
        belongs to the voice task, not this one.
        """
        buffered, self._audio = self._audio, None
        opened, self._audio_session = self._audio_session, None
        if opened is None:
            return  # audioEnd with no burst ever opened: nothing to do, not an error
        if opened != session_id:
            _log.info(
                "xr: audioEnd named session %s but the burst was opened for %s — using the header",
                session_id,
                opened,
            )
        if buffered is None:
            return  # ran past the cap; already answered once with audio_too_long
        hook = TRANSCRIBE
        if hook is None:
            await self._send_frame(
                Error(
                    code="stt_unavailable",
                    message="speech-to-text is not wired up in this build",
                )
            )
            return
        if not buffered:
            await self._send_frame(
                Error(
                    code="stt_empty",
                    message=f"no audio arrived for the burst addressed to {opened}",
                )
            )
            return
        try:
            text = await asyncio.to_thread(hook, bytes(buffered))
        except Exception as exc:
            await self._send_frame(Error(code="stt_failed", message=_one_line(exc)))
            return
        await self._send_frame(Stt(text=text or "", final=True))


# --- helpers --------------------------------------------------------------------


def _deliver(
    project: ProjectInfo, row: TeamSession, label: str | None, text: str
) -> tuple[bool, str]:
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


def _whole_lines(chunk: bytes) -> tuple[list[bytes], bytes]:
    """Split ``chunk`` into complete lines and the unterminated remainder.

    The remainder is what the caller must not consume yet: a writer halfway
    through a record. Returned rather than dropped so the caller can rewind by
    exactly its length.
    """
    cut = chunk.rfind(b"\n")
    if cut == -1:
        return [], chunk
    return chunk[:cut].split(b"\n"), chunk[cut + 1 :]


@dataclass
class _TailState:
    """What :meth:`_Connection._stream_transcript` remembers between polls.

    ``identity`` is ``(st_ino, st_dev)`` of the file being followed, so a
    replacement can be told from growth even when the new file is at least as
    long as the old read offset.
    """

    path: Path
    offset: int
    identity: tuple[int, int]


def _file_identity(handle: BinaryIO) -> tuple[int, int]:
    """``(st_ino, st_dev)`` of an open file: what says it was REPLACED, not grown.

    Size alone misses a transcript swapped for one at least as long as the read
    offset — the shrink check never fires and the tail reads the replacement from
    a stale offset, skipping its leading records. ``os.replace`` and a reused
    ``transcript_path`` both give the new file a different inode, so comparing
    identity across polls catches the swap that a size comparison cannot.
    """
    st = os.fstat(handle.fileno())
    return (st.st_ino, st.st_dev)


def _read_backlog(handle: BinaryIO, size: int, budget: int) -> bytes:
    """Bytes from a record boundary near the end of the file to EOF.

    About ``budget`` bytes of history, but two properties the plain
    ``size - budget`` slice did not have:

    - The returned bytes start at ``0`` or immediately after a newline, so the
      first line is a whole record, not a clipped fragment to drop.
    - They ALWAYS include the last complete record, even when that record alone
      is larger than ``budget``. Slicing at a fixed offset and dropping the
      leading fragment replayed NOTHING when the final record spanned the whole
      window — an 8 KB ``tool_result`` or a long answer — because the window held
      only that record's tail and its terminating newline, and dropping the
      fragment before that newline dropped the record. The focus panel then sat
      on "waiting for transcript…" until the agent wrote a fresh record.

    So the read walks backward in doubling steps until the window holds a
    newline that leaves at least one whole record after it (two newlines: one
    ending the leading fragment, one ending a record), or reaches the start of
    the file. The same bounded read is used for the resync replay, so a replaced
    file restarts within this budget instead of streaming its whole length
    through one unbounded ``read()``.
    """
    if size <= budget:
        handle.seek(0)
        return handle.read()
    step = budget
    while True:
        start = max(0, size - step)
        handle.seek(start)
        data = handle.read()
        if start == 0:
            return data
        if data.count(b"\n") >= 2:
            return data[data.index(b"\n") + 1 :]
        step *= 2


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
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
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
