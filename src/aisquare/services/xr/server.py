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
  its own poll task, its own transcript tail, its own transcriber and its own
  utterance state. A client that vanishes mid-frame, or sends nonsense, or
  asks for a session that is not there, or holds a push-to-talk trigger for a
  minute, affects nothing else — and no agent session is ever blocked on any
  of it. That is non-negotiable #6.
- **Voice is a command path, so the event loop never waits on a model.**
  Every call into :mod:`services.xr.speech` — loading it, feeding it, ending
  an utterance — goes through :func:`asyncio.to_thread`. A whisper decode is
  hundreds of milliseconds and a model load is seconds; either one on the loop
  would stall the 500 ms board poll, and a ring that freezes while the
  operator talks is the failure this whole design is arranged to avoid (§10).

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
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import as_file, files
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from aisquare.core.store import AmbiguousIdError
from aisquare.models import ProjectInfo, TeamSession
from aisquare.services.xr import projector, speech
from aisquare.services.xr.protocol import (
    CLOSE_AUTH_FAILED,
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
from aisquare.services.xr.speech import SpeechUnavailable, Transcriber

_log = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from starlette.applications import Starlette
    from starlette.websockets import WebSocket

POLL_MS = 500
"""Board poll interval. ``AISQUARE_XR_POLL_MS`` overrides it (the suite sets 20)."""

AUTH_TIMEOUT_S = 5.0
"""How long a socket may stay silent before it has authenticated."""

TRANSCRIPT_BACKLOG_BYTES = 8192
"""How much of a transcript to replay on subscribe: the last screen or so."""

MAX_UTTERANCE_S = 60.0
"""How long one push-to-talk burst may run before it is dropped.

A minute is already far past the end of a spoken command — the operator is
holding a trigger down the whole time — so reaching it means the trigger is
stuck, a client is replaying a file, or a headset was put down mid-word.
Dropping is the fail-open answer: the socket stays up, the ring keeps
updating, and the only thing lost is audio nobody was going to act on.
"""

MAX_AUDIO_BYTES = int(speech.SAMPLE_RATE * speech.SAMPLE_BYTES * MAX_UTTERANCE_S)
"""The same cap counted in bytes, which is the one that bounds memory.

Derived from the wire format rather than written out, so it tracks
:data:`MAX_UTTERANCE_S` and the client's frame format instead of drifting from
both. The wall clock alone would bound nothing: a client streaming a file as
fast as the socket will take it can push ten minutes of audio through in
seconds, and the buffer it lands in belongs to the transcriber, where this
module cannot see it grow. Whichever cap trips first ends the utterance.
"""

TranscriberFactory = Callable[[], Transcriber]
"""How a connection gets its :class:`~aisquare.services.xr.speech.Transcriber`.

No arguments and one return, so a test passes
``lambda: FakeTranscriber("open the ring")`` and this module needs to know
nothing about models, extras or downloads. Raising
:class:`~aisquare.services.xr.speech.SpeechUnavailable` is part of the
contract rather than a violation of it: that is how "this machine cannot do
voice, and here is the line that fixes it" reaches the operator.

It replaces the ``TRANSCRIBE`` hook this module shipped with. A module-level
callable that a later task was supposed to assign is a seam that can only be
occupied once, by whoever imports last; a factory is per app, per connection,
and a test can hold two different ones at the same time.
"""

_factory: TranscriberFactory = speech.transcriber
"""The process-wide default. :func:`set_transcriber_factory` replaces it."""


def set_transcriber_factory(factory: TranscriberFactory | None) -> None:
    """Replace the default factory; ``None`` restores the real one.

    The coarse knob, for a caller that builds no app of its own.
    :func:`build_app` takes a per-app factory and ``app.state`` carries it,
    which is what the suite uses — a module-level override is process state,
    and two tests that both set it are two tests that can only be read
    together.
    """
    global _factory
    _factory = factory or speech.transcriber


def transcriber_factory() -> TranscriberFactory:
    """The current process-wide default factory."""
    return _factory


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


def build_app(
    project: ProjectInfo,
    *,
    token: str,
    transcriber_factory: TranscriberFactory | None = None,
) -> Starlette:
    """The ASGI app for one project's board.

    The token is taken as an argument rather than read here so a test can drive
    a wrong one, and so the value is resolved once at startup instead of per
    frame.

    ``transcriber_factory`` is the voice seam. It lands on
    ``app.state.transcriber_factory`` and is read there per connection, so it
    can also be swapped on a running app — which is what makes a test able to
    start with a working backend and then take it away. ``None`` falls back to
    the process-wide default, which is the real faster-whisper one.
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
        await _Connection(
            websocket,
            project=project,
            token=token,
            # Read per connection, not captured once: `app.state` is the
            # documented place to swap this, and a value bound at build time
            # would make that swap silently do nothing.
            transcriber_factory=getattr(app.state, "transcriber_factory", None) or _factory,
        ).serve_client()

    app = Starlette(
        routes=[
            Route("/", index),
            WebSocketRoute("/ws", socket),
            Route("/{path:path}", asset),
        ]
    )
    app.state.transcriber_factory = transcriber_factory
    return app


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


@dataclass
class _Utterance:
    """One push-to-talk burst, from the ``audio`` header to ``audioEnd``.

    ``transcriber`` is ``None`` for a burst that is being ACCEPTED AND
    DISCARDED — speech is unavailable on this machine, or the burst ran past
    its cap, or the backend threw. That is a state, not an error: the client
    has already been told once and cannot un-press the trigger, so the frames
    still arriving have somewhere to go that is neither a transcript nor a
    second complaint.
    """

    session: str
    started: float
    transcriber: Transcriber | None
    audio_bytes: int = 0

    def past_cap(self, *, now: float) -> bool:
        """Whether this burst has run past either cap. See :data:`MAX_AUDIO_BYTES`."""
        return now - self.started > MAX_UTTERANCE_S or self.audio_bytes > MAX_AUDIO_BYTES


class _Connection:
    """One client socket, from the auth frame to the close.

    State lives here and nowhere else, which is the whole fail-open story: two
    headsets on the same board share the SQLite file and nothing else, and
    neither can wedge the other.
    """

    def __init__(
        self,
        websocket: WebSocket,
        *,
        project: ProjectInfo,
        token: str,
        transcriber_factory: TranscriberFactory,
    ) -> None:
        self._ws = websocket
        self._project = project
        self._token = token
        self._make_transcriber = transcriber_factory
        self._sent: list[Session] = []
        self._unread_since: dict[str, int] = {}
        self._board_seq = 0
        self._tail: asyncio.Task[None] | None = None
        self._transcriber: Transcriber | None = None
        self._utterance: _Utterance | None = None
        self._stray_reported = False
        self._transcript_seq = 0

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
            snapshot = projector.snapshot(store, self._project.id, unread_since=self._unread_since)
            self._sent = list(snapshot.sessions)
            await self._send_frame(snapshot)
        poller = asyncio.create_task(self._poll())
        try:
            await self._read_loop()
        finally:
            poller.cancel()
            await self._stop_tail()
            self._discard_utterance("the client disconnected mid-burst")
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
        except TimeoutError:
            # Told apart from every other refusal on purpose, and it is the one
            # exception to "one answer for every way of not being authorised":
            # silence is not a guess. A client that never sent a frame has a
            # transport problem — a reverse-forward that is not up, a socket
            # that opened against the wrong port — and `auth_failed` would send
            # its operator looking for a token that was never the issue.
            await self._reject(
                code="auth_timeout",
                message=f"no auth frame arrived within {AUTH_TIMEOUT_S:.0f}s",
            )
            return False
        except Exception:  # pragma: no cover - client vanished mid-handshake
            return False
        try:
            message = parse_client(raw)
        except (ValidationError, ValueError, KeyError):
            await self._reject()
            return False
        # Imported here, not at module scope: `secrets` pulls in hashlib and
        # ssl, and tests/test_iam_single_reader.py ratchets those out of the
        # import graph the hook path walks on every prompt.
        import secrets

        if not isinstance(message, Auth):
            await self._reject()
            return False
        # Encoded, for the reason `mcp_server._BearerGuard` already documents:
        # str-mode compare_digest raises TypeError on a non-ASCII argument, so
        # a token with one emoji in it would leave this function as an
        # unhandled exception instead of an `auth_failed` frame. UTF-8 both
        # sides keeps the comparison constant-time and total.
        supplied = message.token.encode("utf-8", "surrogatepass")
        if not secrets.compare_digest(supplied, self._token.encode("utf-8", "surrogatepass")):
            await self._reject()
            return False
        return True

    async def _reject(
        self,
        *,
        code: str = "auth_failed",
        message: str = "the first frame must be a valid auth token",
    ) -> None:
        with contextlib.suppress(Exception):
            await self._send_frame(Error(code=code, message=message))
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
        self._board_seq = latest

    def _seed_late_joiners(self, store: Any) -> None:
        """Watermark a session that first appeared AFTER this client connected.

        :meth:`_seed_watermarks` only sees the sessions that exist at connect,
        and :func:`projector._unread_counts` counts nothing for an id it holds
        no watermark for. So a session spawned while the operator is wearing
        the headset reported 0 unread forever, no matter how loudly it worked —
        which is the exact case the badge exists for, and the one most likely
        to happen during a demo, because that is when agents get spawned.

        The watermark is the board position this connection had ALREADY seen,
        not the current head and not 0. Not the head, because the events that
        announced the session arrived in the same tick that revealed it and
        would be swallowed; not 0, because that empties the session's entire
        history into a badge meant to say "since you looked".
        """
        latest = store.latest_seq(self._project.id)
        for row in store.team_sessions(self._project.id):
            if row.id not in self._unread_since:
                self._unread_since[row.id] = self._board_seq
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

        It is also the connection's ONLY clock, which is why the utterance cap
        is re-checked here and not just as frames arrive. A burst that is
        opened and then abandoned — the headset put down mid-word that
        :data:`MAX_UTTERANCE_S` names — sends no further frames, so a check
        that only runs on arrival never runs again and the loaded transcriber
        is held for the life of the socket. The resolution is one poll interval
        rather than exact, which is the right precision for a cap whose job is
        to bound a leak rather than to time anything.
        """
        interval = poll_interval()
        while True:
            await asyncio.sleep(interval)
            idle = self._utterance
            if (
                idle is not None
                and idle.transcriber is not None
                and idle.past_cap(now=time.monotonic())
            ):
                try:
                    await self._drop_past_cap(idle)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    return
            try:
                with _store() as store:
                    self._seed_late_joiners(store)
                    current = projector.sessions(
                        store, self._project.id, unread_since=self._unread_since
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
                await self._on_audio_frame(data)
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
            await self._open_utterance(message)
        elif isinstance(message, AudioEnd):
            await self._close_utterance(message.session)
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
        if session_id is None:
            return
        with _store() as store:
            try:
                row = store.get_session(session_id)
            except AmbiguousIdError as exc:
                # A prefix that matches two sessions is the CLIENT's mistake.
                # Left to escape, it reaches _read_loop's catch-all and the
                # headset is told `internal` — a server fault — for a string it
                # can fix by typing one more character.
                await self._send_frame(Error(code="ambiguous_session", message=_one_line(exc)))
                return
            if row is not None and row.project_id == self._project.id:
                # Focusing a panel is what marks it read, keyed on the id the
                # BOARD uses. `get_session` resolves PREFIXES, and every frame
                # that goes back out carries `row.id` — so keying this on the
                # client's string watermarks an id nothing ever counts, leaving
                # the badge frozen at whatever it said and the short string in
                # the map for the life of the socket. That is the outcome the
                # rest of this comment promises does not happen: a watermark
                # for a session that does not exist ON THIS BOARD would sit in
                # the map forever, counting nothing.
                self._unread_since[row.id] = store.latest_seq(self._project.id)
        if row is None or row.project_id != self._project.id:
            await self._send_frame(
                Error(code="no_such_session", message=f"no session {session_id} on this board")
            )
            return
        if not row.transcript_path:
            await self._send_frame(
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
        """Replay the tail of a transcript, then follow it as it grows.

        Backlog first (:data:`TRANSCRIPT_BACKLOG_BYTES` from the end, whole
        records only), then a poll on the same interval as the board. Polling
        rather than inotify because the file may be on any filesystem and this
        is a text stream a human reads, not a frame budget.

        **Only whole lines are consumed.** A JSONL record here is one turn of a
        conversation — kilobytes — and a poll that lands mid-write sees a
        fragment. Splitting what arrived and advancing past all of it would
        parse that fragment as nothing (correct) and then never see the rest
        (wrong): the record is dropped, silently, and the panel skips a turn.
        So the offset rewinds to the last newline and the fragment is re-read
        next tick, when it is whole.
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
            await self._send_frame(Error(code="no_transcript", message=_one_line(exc)))
            return
        complete, partial = _whole_lines(backlog)
        offset -= len(partial)
        if start > 0 and complete:
            complete = complete[1:]  # the first line is a fragment of a clipped record
        for line in complete:
            await self._emit_record(row.id, line)
        while True:
            await asyncio.sleep(interval)
            try:
                with path.open("rb") as handle:
                    handle.seek(0, os.SEEK_END)
                    if handle.tell() < offset:
                        # The file got SHORTER than what has already been read:
                        # a compaction, a `/clear` onto the same path, a
                        # rotation, or a new session handed the same
                        # `transcript_path`. The offset only ever moved
                        # forward, so seeking to it now reads b"" on every tick
                        # from here to the end of the connection — no frame, no
                        # error, no close. The operator is left looking at a
                        # live ring beside a conversation that stopped, with
                        # nothing anywhere saying why, and re-subscribing is
                        # the only way back. Resync to the start of whatever is
                        # there now, which for every case in that list is the
                        # first record of the file that replaced it.
                        offset = 0
                    handle.seek(offset)
                    fresh = handle.read()
            except OSError:
                return
            if not fresh:
                continue
            complete, partial = _whole_lines(fresh)
            offset += len(fresh) - len(partial)
            for line in complete:
                await self._emit_record(row.id, line)

    async def _emit_record(self, session_id: str, line: bytes) -> None:
        text = _record_text(line)
        if not text:
            return
        self._transcript_seq += 1
        await self._send_frame(
            Transcript(session=session_id, seq=self._transcript_seq, text=text, final=True)
        )

    # -- prompts

    async def _prompt(self, message: Prompt) -> None:
        """A typed ``prompt`` frame. The routing itself is :meth:`_route`."""
        await self._send_frame(await self._route(message.session, message.text))

    async def _route(self, session_id: str, text: str) -> Ack:
        """Send text to one session as the operator, and say how it landed.

        Two paths, and the client is told which. A session with a live fleet
        pane goes through :func:`services.fleet.tell`, which types into a
        WAITING pane and otherwise files a board note — its own answer is
        reported verbatim in ``ack.detail``. A session with no pane (an MCP
        client, an agent someone started by hand) has nothing to type into, so
        the note is filed directly, addressed to its label or role, where the
        agent's next delta injection delivers it.

        Shared by the typed path and the voice one, which is the point: a
        spoken command and a typed one must reach the agent by the same route
        with the same answer, or "say it instead of typing it" quietly means
        something else. Returns the ``ack`` rather than sending it, because the
        voice path has an ``stt`` frame to put on the wire first.
        """
        text = text.strip()
        if not text:
            return Ack(session=session_id, ok=False, detail="refusing to send an empty prompt")
        with _store() as store:
            row = store.get_session(session_id)
            agent = next(
                (
                    candidate
                    for candidate in store.fleet_agents(self._project.id, live_only=True)
                    if candidate.session_id == session_id
                ),
                None,
            )
        if row is None or row.project_id != self._project.id:
            return Ack(session=session_id, ok=False, detail="no such session on this board")
        ok, detail = await asyncio.to_thread(
            _deliver, self._project, row, agent.label if agent is not None else None, text
        )
        return Ack(session=session_id, ok=ok, detail=detail)

    # -- audio

    async def _open_utterance(self, message: Audio) -> None:
        """Start a burst for one session, and get a transcriber for it.

        The factory runs in a thread for the same reason every ``feed`` does:
        the real one loads a whisper model, which is the most expensive call in
        this file by two orders of magnitude, and doing it on the loop would
        stop the board dead for seconds the first time anyone speaks.

        A WORKING transcriber is cached for the connection and reused by every
        later utterance — that is what the model load is paid for, and
        :class:`~aisquare.services.xr.speech.BufferedTranscriber` resets itself
        in ``finish()``. A FAILING factory is deliberately not cached: it is
        retried on the next press, so an operator who installs the extra while
        the headset is still on gets voice back without reconnecting.
        """
        self._discard_utterance("a second audio header arrived before audioEnd")
        self._stray_reported = False
        started = time.monotonic()
        try:
            transcriber = await self._ensure_transcriber()
        except SpeechUnavailable as exc:
            await self._open_discarding(message.session, started, f"{exc.reason} — {exc.fix}")
            return
        except Exception as exc:  # a broken factory must not cost the socket
            await self._open_discarding(message.session, started, _one_line(exc))
            return
        self._utterance = _Utterance(message.session, started, transcriber)

    async def _open_discarding(self, session_id: str, started: float, message: str) -> None:
        """Open an utterance that goes nowhere, and say why exactly once.

        The frames are already in flight — the operator is mid-sentence and the
        client cannot un-press the trigger — so they are accepted and dropped
        rather than refused one by one. Everything else on the socket keeps
        working while this happens, which is what fail-open means here: the
        ring still turns, deltas still arrive, typing still routes.
        """
        self._utterance = _Utterance(session_id, started, None)
        await self._send_frame(Error(code="stt_unavailable", message=message))

    async def _ensure_transcriber(self) -> Transcriber:
        """The connection's transcriber, loaded once, off the event loop."""
        if self._transcriber is None:
            self._transcriber = await asyncio.to_thread(self._make_transcriber)
        return self._transcriber

    async def _on_audio_frame(self, chunk: bytes) -> None:
        """One binary frame: 16 kHz mono PCM16LE, in order, any size.

        The client sends 20 ms (640 bytes) at a time, but nothing here depends
        on that. A websocket implementation may coalesce or split frames on its
        own, including mid-sample, and
        :class:`~aisquare.services.xr.speech.BufferedTranscriber` carries a
        trailing odd byte into the next chunk rather than concatenating it raw
        — so the size really is the client's business, and the ORDER is the
        only thing this path needs. That carry is load-bearing for this
        sentence: without it one odd-length frame would flip the buffer's
        parity permanently and every decode after it would raise.
        """
        utterance = self._utterance
        if utterance is None:
            await self._report_stray()
            return
        if utterance.transcriber is None:
            return  # accepted and discarded; this utterance was already answered
        utterance.audio_bytes += len(chunk)
        if utterance.past_cap(now=time.monotonic()):
            await self._drop_past_cap(utterance)
            return
        if not chunk:
            return
        try:
            interim = await asyncio.to_thread(utterance.transcriber.feed, chunk)
        except Exception as exc:
            await self._fail_utterance(utterance, exc)
            return
        if interim:
            await self._send_frame(Stt(text=interim, final=False))

    async def _report_stray(self) -> None:
        """Answer the FIRST stray binary frame, then stay quiet until a header.

        Once, not once per frame. A client whose ``audio`` header was lost
        sends dozens more before it could possibly react, and dozens of
        identical errors would bury the one frame the operator needed to read
        under a flood the server generated itself. The latch lifts on the next
        header, so the next genuine mistake is reported like the first.
        """
        if self._stray_reported:
            return
        self._stray_reported = True
        await self._send_frame(
            Error(
                code="audio_unexpected",
                message="binary audio arrived with no open utterance — send an audio header first",
            )
        )

    async def _drop_past_cap(self, utterance: _Utterance) -> None:
        """Past :data:`MAX_UTTERANCE_S`: say so once, swallow the rest.

        Reached from both clocks — the next frame to arrive, and the poller
        when none does. Idempotent by the ``transcriber = None`` below, which
        is what keeps a burst that trips both from being answered twice.
        """
        self._forget_transcriber()
        utterance.transcriber = None
        await self._send_frame(
            Error(
                code="audio_too_long",
                message=(
                    f"the utterance ran past {MAX_UTTERANCE_S:.0f}s and was dropped — "
                    "release the trigger and say it again"
                ),
            )
        )

    async def _fail_utterance(self, utterance: _Utterance, exc: Exception) -> None:
        """The backend raised mid-utterance: drop it, keep the socket.

        ``stt_failed`` is the code this module already used for a backend that
        threw, kept rather than renamed. A decode that crashes is not one of
        the four failure modes the voice contract enumerates, and answering it
        with silence would be the one outcome worse than any of them: a mic
        that looks live and produces nothing, with no line anywhere saying why.
        """
        self._forget_transcriber()
        utterance.transcriber = None
        await self._send_frame(Error(code="stt_failed", message=_one_line(exc)))

    def _forget_transcriber(self) -> None:
        """Drop the cached transcriber, because it may be unusable — not merely stale.

        THE REASON IS CORRECTNESS, and it has to be said first, because the
        cost argument below reads like the whole story and invites the refactor
        that reintroduces the bug: *we only drop this to save a decode; on a
        decode FAILURE there is no decode to save, so keep the model.* That is
        a pure win right up until the failure was a mid-utterance raise, which
        leaves the transcriber holding the buffer that caused it. Reusing it
        then costs the operator voice for the REST OF THE CONNECTION rather
        than one sentence, and it surfaces as a socket that looks healthy and
        transcribes nothing. A fresh object is the only state this connection
        can reason about, so every path that abandons an utterance drops it.
        ``tests/test_xr_server.py`` pins this: with the line below removed, an
        odd-length frame anywhere in one burst kills the next burst too.

        Cost agrees, which is why there is nothing to trade off. ``finish()``
        is what resets a
        :class:`~aisquare.services.xr.speech.BufferedTranscriber`, and calling
        it here would run a decode over up to a minute of audio for the sole
        purpose of throwing the answer away. Dropping the object costs the NEXT
        utterance a model load instead, which is the right way round: the
        abusive path pays for itself, and the common one — press, speak,
        release, press again — keeps the model it already loaded.
        """
        self._transcriber = None

    def _discard_utterance(self, reason: str) -> None:
        """Forget an open burst without transcribing it, and log the one line.

        One line, at info, naming the session: a headset taken off mid-sentence
        is ordinary and must not produce a traceback, but a burst that vanishes
        with no record at all is the thing nobody can debug afterwards.
        """
        if self._utterance is None:
            return
        _log.info(
            "xr: dropped a voice utterance for session %s — %s", self._utterance.session, reason
        )
        self._utterance = None
        self._forget_transcriber()

    async def _close_utterance(self, session_id: str) -> None:
        """``audioEnd``: the final transcript, then route it exactly like a prompt.

        The client does not echo a ``prompt`` for voice, and that is the
        design rather than a shortcut: the operator committed when they
        released the trigger, and a round trip through the headset to ask them
        to confirm what they just said is exactly the latency §10 exists to
        remove. The interim frames are what let them see it coming.

        An ``audioEnd`` with no open utterance is ignored rather than answered.
        The client sending one is usually a client whose burst this server has
        already dropped — it cannot know that yet — and an error there would
        report the server's own decision back as the client's mistake.
        """
        utterance, self._utterance = self._utterance, None
        self._stray_reported = False
        if utterance is None or utterance.transcriber is None:
            return
        if utterance.session != session_id:
            # The header opened the burst and the audio was recorded for it, so
            # the header wins. Logged rather than corrected: a client whose two
            # frames disagree has a bug worth finding.
            _log.info(
                "xr: audioEnd named session %s but the burst was opened for %s — using the header",
                session_id,
                utterance.session,
            )
        try:
            text = (await asyncio.to_thread(utterance.transcriber.finish)).strip()
        except Exception as exc:
            await self._fail_utterance(utterance, exc)
            return
        await self._send_frame(Stt(text=text, final=True))
        if not text:
            # Silence is not a prompt. `speech`'s gate already refuses to
            # invent one out of room tone, and routing "" from here would file
            # an empty board note against an operator who simply did not speak.
            return
        await self._send_frame(await self._route(utterance.session, text))


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
    "MAX_AUDIO_BYTES",
    "MAX_UTTERANCE_S",
    "POLL_MS",
    "TranscriberFactory",
    "build_app",
    "port_in_use",
    "run",
    "set_transcriber_factory",
    "transcriber_factory",
    "web_root",
)
