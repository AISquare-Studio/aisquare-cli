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
- **Voice is a command path, so neither the event loop nor the read loop
  ever waits on a model.** Every call into :mod:`services.xr.speech` —
  loading it, feeding it, ending an utterance — runs on a thread, and it is
  issued from a per-connection WORKER task fed by a queue in wire order, not
  from the socket's read loop. A whisper decode is hundreds of milliseconds
  and a model load is seconds to minutes (a first download of ``base.en``
  measured 23-30 s, ``small.en`` 70-99 s); on the event loop either would
  stall the 500 ms board poll, and inline in the read loop — where this
  module first put them — they stopped the socket being read at all: uvicorn
  pauses reading once a frame is queued and the app is not receiving, so the
  client's pongs went unread and the default keepalive closed the socket with
  1011 after 40 s of a model download (measured: 4 of 5 clients), a decode
  slower than the one-second interim cadence let intake fall behind real time
  until the wall-clock cap dropped an in-contract press, and every typed
  prompt and ``audioEnd`` waited behind a decode. With the worker the read
  loop only counts bytes, applies the caps and enqueues; frames that arrive
  during a slow decode are fed as ONE chunk when the worker comes back, so it
  catches up instead of falling further behind. The threads are daemon
  threads that nothing joins (:func:`_in_thread`), which is what lets Ctrl-C
  stop the server while a download is in flight.

What this module does NOT do: it never writes to the board except through the
two public service entry points a prompt reaches (``fleet.tell``,
``team.add_note``), it touches no hook, no task lifecycle call, and no
settings file.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import socket
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import as_file, files
from pathlib import Path
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from pydantic import ValidationError

from aisquare.models import ProjectInfo, TeamSession
from aisquare.services.xr import projector, speech
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
from aisquare.services.xr.speech import SpeechUnavailable, Transcriber

_log = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from starlette.applications import Starlette
    from starlette.websockets import WebSocket

POLL_MS = 500
"""Board poll interval. ``AISQUARE_XR_POLL_MS`` overrides it (the suite sets 20)."""

AUTH_TIMEOUT_S = 5.0
"""How long a socket may stay silent before it has authenticated."""

CLOSE_AUTH_FAILED = 4401
"""Application close code for a failed auth. 4000-4999 is the private range."""

CLOSE_TRY_AGAIN_LATER = 1013
"""Close code for a board that could not be read at connect.

RFC 6455's "try again later", and it is chosen for what the client should do
next: a locked or damaged ``context.db`` during the hello/snapshot read is a
transient of the server's, so reconnecting with backoff is right — unlike
:data:`CLOSE_AUTH_FAILED`, where it is wrong. The client is told why in a
``board_unavailable`` error frame first; before that frame existed the store
error escaped the ASGI app as a traceback and the transport was dropped with
no close frame at all, which the client could only read as a network fault.
"""

CLOSE_SERVICE_RESTART = 1012
"""The close code uvicorn hands the app for every open socket when it shuts down.

All three of its websocket implementations deliver ``websocket.disconnect``
with this code from ``shutdown()`` and nothing a client does produces it, so
it is how a connection tells "the operator hit Ctrl-C" apart from "the headset
went away" — the two cases :data:`VOICE_DRAIN_S` treats differently.
"""

VOICE_DRAIN_S = 30.0
"""How long a connection the CLIENT closed keeps working on a burst it committed.

Releasing the trigger is the commit: from ``audioEnd`` onward the sentence is
the operator's instruction, and a headset that then loses its socket — walked
out of range, closed the page, the tether pulled — has not un-said it. So the
voice worker is drained rather than cancelled when the socket goes, bounded
by this many seconds: a final decode over the longest legal burst runs a few
seconds on CPU, and a model that was still downloading when the client left
gets the rest of this budget and no more. A burst still OPEN at the
disconnect is discarded, with one line at info: nobody released the trigger,
so nothing was committed.

None of this applies when the SERVER is the one going away
(:data:`CLOSE_SERVICE_RESTART`): then the worker is cancelled at once, because
a drain here is the second way Ctrl-C can be ignored for the length of a
model download — measured: with the drain applied to a shutdown close, exit
came 12.1 s after SIGINT with a 12 s factory in flight, the same number the
executor join produced before :func:`_in_thread`.
"""

MAX_UTTERANCE_S = 60.0
"""How long one push-to-talk burst may run before it is dropped.

A minute is already far past the end of a spoken command — the operator is
holding a trigger down the whole time — so reaching it means the trigger is
stuck, a client is replaying a file, or a headset was put down mid-word.
Dropping is the fail-open answer: the socket stays up, the ring keeps
updating, and the only thing lost is audio nobody was going to act on.

Wall-clock time since the ``audio`` header ARRIVED, checked as each frame
arrives and from the poller when none does. It measures the press, and only
the press, because the read loop never waits on a decode: when it did, a
decode slower than the interim cadence made intake lag real time and this
clock dropped a 46 s press at 60 s, fourteen seconds after the operator had
released the trigger.
"""

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

ONE seam: :func:`build_app` takes it and ``app.state.transcriber_factory``
carries it, read per connection. It replaces the ``TRANSCRIBE`` hook this
module shipped with — a module-level callable a later task was supposed to
assign, which can only be occupied once, by whoever imports last — and it
also replaced the process-wide ``set_transcriber_factory`` default that
briefly sat beside it: two ways to inject the same object are two places a
reader has to check to learn which transcriber a socket will get.
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
    start with a working backend and then take it away. ``None`` means the
    real faster-whisper one, :func:`speech.transcriber`.
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
            transcriber_factory=app.state.transcriber_factory,
        ).serve_client()

    app = Starlette(
        routes=[
            Route("/", index),
            WebSocketRoute("/ws", socket),
            Route("/{path:path}", asset),
        ]
    )
    app.state.transcriber_factory = transcriber_factory or speech.transcriber
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

    Shared between the read loop, which creates it and counts its bytes as
    they arrive, and the voice worker, which decodes it later — so the one
    thing both need to agree on is ``dropped``. It holds the error code this
    burst was answered with, or ``None`` while the burst is live. A dropped
    burst is ACCEPTED AND DISCARDED: speech is unavailable on this machine,
    or it ran past its cap, or a frame was not sample-aligned, or the backend
    threw. That is a state, not a second error — the client has already been
    told once and cannot un-press the trigger, so the frames still arriving
    have somewhere to go that is neither a transcript nor a second complaint.
    Every path that drops goes through :meth:`_Connection._drop`, which sets
    the code only once, and that is what makes "answered exactly once" hold
    across both clocks and both tasks: the worker checks it again after every
    thread call, so a cap that trips during a decode produces no stale
    interim and no second ``stt_failed`` behind the ``audio_too_long``.
    """

    session: str
    seq: int
    started: float
    audio_bytes: int = 0
    dropped: str | None = None

    def past_cap(self, *, now: float) -> bool:
        """Whether this burst has run past either cap. See :data:`MAX_AUDIO_BYTES`."""
        return now - self.started > MAX_UTTERANCE_S or self.audio_bytes > MAX_AUDIO_BYTES


@dataclass(frozen=True)
class _Open:
    """Worker item: a burst began; make sure the connection has a transcriber."""

    utterance: _Utterance


@dataclass(frozen=True)
class _Frame:
    """Worker item: one binary frame of a burst, in the order it arrived."""

    utterance: _Utterance
    chunk: bytes


@dataclass(frozen=True)
class _End:
    """Worker item: the burst was committed; decode it whole and route it."""

    utterance: _Utterance


class _Stop:
    """Worker item: everything before this has been handled; the worker may return."""


_VoiceItem = _Open | _Frame | _End | _Stop


class _VoiceQueue:
    """Wire-ordered work for the voice worker, with frames coalescable at the head.

    A ``deque`` and an event rather than :class:`asyncio.Queue`, because the
    worker needs one thing a queue does not offer: to take EVERY frame of the
    same burst that is already waiting, as one chunk, so that a decode that
    ran long is followed by one ``feed`` of everything that arrived meanwhile
    rather than one per frame. That is what lets the worker catch up after a
    slow decode instead of staying one decode behind for the rest of the
    press. Order is the deque's, which is arrival order, and it is pinned by
    ``tests/test_xr_server.py`` with frames whose payloads differ.
    """

    def __init__(self) -> None:
        self._items: deque[_VoiceItem] = deque()
        self._ready = asyncio.Event()

    def put(self, item: _VoiceItem) -> None:
        self._items.append(item)
        self._ready.set()

    async def take(self) -> _VoiceItem:
        while not self._items:
            self._ready.clear()
            await self._ready.wait()
        return self._items.popleft()

    def take_frames_of(self, utterance: _Utterance) -> list[bytes]:
        """Pop the frames of ``utterance`` waiting at the head, in order."""
        chunks: list[bytes] = []
        while self._items:
            head = self._items[0]
            if not isinstance(head, _Frame) or head.utterance is not utterance:
                break
            chunks.append(head.chunk)
            self._items.popleft()
        return chunks


_P = ParamSpec("_P")
_T = TypeVar("_T")


async def _in_thread(fn: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> _T:
    """Run ``fn`` on a daemon thread and await its result. Nothing joins the thread.

    Not :func:`asyncio.to_thread`, and the difference is what Ctrl-C does.
    The default executor is joined when the loop shuts down, so a model load
    or a long decode in flight held the process after the operator asked it
    to stop: measured through ``uvicorn.run`` with a 12 s factory, exit came
    12.03 s after SIGINT, and ``cli/xr.py``'s banner says "Ctrl-C stops". A
    daemon thread is not joined by anything — the interpreter exits without
    it — and the same measurement with this function is 0.2 s. The cost is
    that a decode can be cut off mid-way at exit, which is what the operator
    asked for.

    The result crosses back through ``call_soon_threadsafe``. A future that
    was cancelled meanwhile — the connection went away and its worker was
    torn down — is left alone rather than set, and a loop that has already
    closed raises ``RuntimeError`` from the call, which is swallowed: in both
    cases nobody is waiting for the answer.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[_T] = loop.create_future()

    def deliver(settle: Callable[[], object]) -> None:
        if not future.done():
            settle()

    def run() -> None:
        settle: Callable[[], object]
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:  # handed to the awaiting task, never lost
            settle = functools.partial(future.set_exception, exc)
        else:
            settle = functools.partial(future.set_result, result)
        with contextlib.suppress(RuntimeError):  # the loop is closed: nobody is waiting
            loop.call_soon_threadsafe(deliver, settle)

    threading.Thread(target=run, name="xr-speech", daemon=True).start()
    return await future


_TOO_LONG = (
    f"the utterance ran past {MAX_UTTERANCE_S:.0f}s and was dropped — "
    "release the trigger and say it again"
)


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
        self._tail: asyncio.Task[None] | None = None
        self._transcript_seq = 0
        self._gone = False
        self._close_code: int | None = None
        # Voice. `_utterance` is the burst being RECEIVED, owned by the read
        # loop; `_voice` carries its frames to the worker in wire order;
        # `_transcriber` and `_held` — the burst whose audio it is holding —
        # are touched by the worker alone, which is what keeps every call
        # into the backend sequential without a lock.
        self._utterance: _Utterance | None = None
        self._voice = _VoiceQueue()
        self._worker: asyncio.Task[None] | None = None
        self._transcriber: Transcriber | None = None
        self._held: _Utterance | None = None
        self._stray_reported = False

    # -- lifecycle

    async def serve_client(self) -> None:
        """Accept, authenticate, then serve until the client goes away.

        Not ``run`` for the same reason ``_send_frame`` is not ``_send``.
        """
        await self._ws.accept()
        if not await self._authenticate():
            return
        try:
            with _store() as store:
                self._seed_watermarks(store)
                snapshot = projector.snapshot(
                    store, self._project.id, unread_since=self._unread_since
                )
        except Exception as exc:
            # A locked or damaged context.db at the one read that cannot be
            # retried by the poller. Left to escape, it was a traceback out
            # of the ASGI app and a transport dropped with no close frame —
            # which a client can only read as a network fault. The frame
            # says what happened and the close code says what to do.
            await self._send_frame(Error(code="board_unavailable", message=_one_line(exc)))
            with contextlib.suppress(Exception):
                await self._ws.close(code=CLOSE_TRY_AGAIN_LATER)
            return
        await self._send_frame(
            Hello(
                protocol=PROTOCOL_VERSION,
                hub=self._project.id,
                server_time=datetime.now(tz=UTC).isoformat(),
            )
        )
        self._sent = list(snapshot.sessions)
        await self._send_frame(snapshot)
        poller = asyncio.create_task(self._poll())
        self._worker = asyncio.create_task(self._voice_worker())
        try:
            await self._read_loop()
        finally:
            self._gone = True
            poller.cancel()
            await self._stop_tail()
            self._abandon_open_burst()
            await self._drain_voice()
            with contextlib.suppress(asyncio.CancelledError):
                await poller

    async def _authenticate(self) -> bool:
        """First frame, within :data:`AUTH_TIMEOUT_S`, or the socket closes.

        Every refusal is the same answer — an ``auth_failed`` error frame and
        close ``4401`` — whether the token was absent, wrong, minted against a
        different ``AISQUARE_HOME``, or the frame was not an ``auth`` at all,
        including a BINARY first frame. Distinguishing them for the caller
        would only help someone guessing. The one thing told apart is
        silence: no frame at all within the timeout is ``auth_timeout``, for
        the reason at that branch. A client that disconnects during the
        handshake gets nothing, because there is nobody to answer.

        The raw ``receive()`` rather than ``receive_text()`` is what makes the
        binary case an answer at all: ``receive_text`` raises ``KeyError`` for
        a message with no ``text``, and that ``KeyError`` came from the
        receive, not the parse — so it fell into the "client vanished" branch
        and the socket was dropped with no frame and no close code, which the
        shipped client reads as a transient and retries forever.
        """
        from starlette.websockets import WebSocketDisconnect

        try:
            packet = await asyncio.wait_for(self._ws.receive(), timeout=AUTH_TIMEOUT_S)
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
        except (WebSocketDisconnect, RuntimeError):  # pragma: no cover - vanished mid-handshake
            return False
        if packet.get("type") == "websocket.disconnect":
            return False
        raw = packet.get("text")
        if not isinstance(raw, str):
            await self._reject()  # a binary first frame is not an auth
            return False
        try:
            message = parse_client(raw)
        except (ValidationError, ValueError):
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

        QUIET ONCE THE CLIENT IS GONE. A headset that closes its socket while
        a decode is in flight, or right after releasing the trigger, is an
        ordinary event, and every send that follows it — the final ``stt``,
        the ``ack``, the poller's next delta — used to raise
        ``WebSocketDisconnect`` out of the ASGI app, which uvicorn prints as
        "ERROR: Exception in ASGI application" with a traceback and then a
        second one, "Cannot call send once a close message has been sent",
        from the error frame the read loop tried to answer it with. Worse, the
        raise skipped whatever came after the send, which for a voice burst
        was the routing of a sentence the operator had already committed. So
        the payload is built outside the guard (a model that cannot be
        serialised is a bug and must raise), and a send that fails for any
        reason marks the client gone, says so once at info, and every later
        send returns without trying: the socket is the only thing a failed
        write can mean.
        """
        if self._gone:
            return
        payload = to_wire(message)
        try:
            await self._ws.send_text(payload)
        except Exception as exc:  # the client is gone, one way or another
            self._gone = True
            _log.info(
                "xr: the client went away while a %s frame was being sent (%s)",
                getattr(message, "t", "?"),
                _one_line(exc),
            )

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
            if idle is not None and idle.dropped is None and idle.past_cap(now=time.monotonic()):
                await self._drop(idle, "audio_too_long", _TOO_LONG)
            try:
                with _store() as store:
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
                self._close_code = packet.get("code")
                return
            data = packet.get("bytes")
            if data is not None:
                try:
                    await self._on_audio_frame(data)
                except Exception as exc:  # a bad frame must not kill the socket either
                    await self._send_frame(Error(code="internal", message=_one_line(exc)))
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
            row = store.get_session(session_id)
            if row is not None:
                # Focusing a panel is what marks it read. Only for a session
                # that exists: a watermark for one that does not would sit in
                # the map forever, counting nothing.
                self._unread_since[session_id] = store.latest_seq(self._project.id)
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

    # -- audio: the read loop's half. Counts, caps, answers, enqueues. Never decodes.

    async def _open_utterance(self, message: Audio) -> None:
        """An ``audio`` header: start a burst, and ask the worker for a transcriber.

        A header that arrives while a burst is still open ENDS that burst
        exactly as its own ``audioEnd`` would have, then opens the new one.
        The shipped client produces this wire — ``audio`` seq 1, frames,
        ``audio`` seq 2, ``audioEnd`` — on a trigger bounce or a quick
        re-press, because release clears its capturing flag before the
        worklet's 250 ms flush has sent ``audioEnd``. Discarding the open
        burst there, which is what this method first did, lost the operator's
        first sentence with nothing on the wire saying so, and dropped the
        loaded model with it. The late ``audioEnd`` then closes burst #2,
        which is the burst it belongs to.
        """
        previous = self._utterance
        if previous is not None and previous.dropped is None:
            _log.info(
                "xr: audio header seq %d arrived while burst seq %d was open — ending the "
                "open burst as if its audioEnd had arrived",
                message.seq,
                previous.seq,
            )
            await self._end_burst(previous)
        self._stray_reported = False
        utterance = _Utterance(session=message.session, seq=message.seq, started=time.monotonic())
        self._utterance = utterance
        self._voice.put(_Open(utterance))

    async def _close_utterance(self, session_id: str) -> None:
        """``audioEnd``: the burst is committed; the worker decodes and routes it.

        An ``audioEnd`` with no open utterance is ignored rather than answered.
        The client sending one is usually a client whose burst this server has
        already dropped — it cannot know that yet — and an error there would
        report the server's own decision back as the client's mistake.
        """
        utterance, self._utterance = self._utterance, None
        self._stray_reported = False
        if utterance is None or utterance.dropped is not None:
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
        await self._end_burst(utterance)

    async def _end_burst(self, utterance: _Utterance) -> None:
        """Commit a burst: the worker answers it, after everything queued before it.

        Even a burst with no audio in it is answered by the worker rather
        than here, so that bursts are answered in the order they were sent: a
        re-press that ends burst #1 and opens an empty burst #2 gets #1's
        final and ack, then #2's ``stt_empty`` — not the error first, while
        #1 is still being decoded.
        """
        if self._utterance is utterance:
            self._utterance = None
        self._voice.put(_End(utterance))

    async def _on_audio_frame(self, chunk: bytes) -> None:
        """One binary frame: 16 kHz mono PCM16LE, in order, a whole number of samples.

        The client sends 20 ms (640 bytes) at a time, but nothing here depends
        on the SIZE — only on the alignment. A websocket delivers whole
        messages, so a frame is exactly what the client built, and a client
        that built an odd-length one has lost or added a byte: every sample
        after it would be read from the wrong pair. Carrying the odd byte into
        the next frame, which this path did for one release, turned that
        client's speech into byte-shifted noise that the model transcribed
        with confidence and this server routed as a prompt. So an odd frame
        is a protocol error, answered once with ``audio_misaligned`` and the
        burst dropped — like ``audio_unexpected``, and unlike a backend crash,
        it costs the burst and not the loaded model.

        Everything else here is bookkeeping on the loop: bytes are counted at
        arrival so the caps measure the press, and the frame is queued for the
        worker in the order it came. Nothing waits on a decode.
        """
        utterance = self._utterance
        if utterance is None:
            await self._report_stray()
            return
        if utterance.dropped is not None:
            return  # accepted and discarded; this utterance was already answered
        if len(chunk) % speech.SAMPLE_BYTES:
            await self._drop(
                utterance,
                "audio_misaligned",
                f"a {len(chunk)}-byte frame is not a whole number of "
                f"{speech.SAMPLE_BYTES}-byte samples, so every sample after it would be "
                "misread — the burst was dropped; send sample-aligned frames and say it again",
            )
            return
        utterance.audio_bytes += len(chunk)
        if utterance.past_cap(now=time.monotonic()):
            await self._drop(utterance, "audio_too_long", _TOO_LONG)
            return
        if chunk:
            self._voice.put(_Frame(utterance, chunk))

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

    async def _drop(self, utterance: _Utterance, code: str, message: str) -> None:
        """Answer a burst with one error and swallow the rest of it.

        Reached from both clocks and both tasks — the next frame to arrive,
        the poller when none does, the worker when a load or a decode fails —
        and idempotent by the ``dropped`` check, which is what keeps a burst
        that trips two of them from being answered twice. Nothing is sent to
        the transcriber here: the worker discards whatever it is holding for
        this burst the next time it looks, without a decode.
        """
        if utterance.dropped is not None:
            return
        utterance.dropped = code
        await self._send_frame(Error(code=code, message=message))

    def _abandon_open_burst(self) -> None:
        """The client is gone mid-burst: nothing was committed, so nothing is decoded.

        One line, at info, naming the session: a headset taken off mid-sentence
        is ordinary and must not produce a traceback, but a burst that vanishes
        with no record at all is the thing nobody can debug afterwards.
        """
        utterance, self._utterance = self._utterance, None
        if utterance is None or utterance.dropped is not None:
            return
        utterance.dropped = "disconnected"
        _log.info(
            "xr: dropped a voice utterance for session %s — the client disconnected mid-burst",
            utterance.session,
        )

    # -- audio: the worker's half. Every call into the transcriber, in wire order.

    async def _voice_worker(self) -> None:
        """Drain :attr:`_voice` for the life of the connection, then until ``_Stop``.

        One task per connection, so every ``feed``/``finish``/factory call is
        sequential without a lock, and off the read loop, so none of them
        stops the socket being read. A bug in a handler is reported to the
        client as ``internal`` and the worker goes on: voice for this
        connection must not silently end because one item raised.
        """
        while True:
            item = await self._voice.take()
            if isinstance(item, _Stop):
                return
            try:
                if isinstance(item, _Open):
                    await self._load_for(item.utterance)
                elif isinstance(item, _Frame):
                    await self._feed(item.utterance, item.chunk)
                else:
                    await self._finish(item.utterance)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._send_frame(Error(code="internal", message=_one_line(exc)))

    async def _drain_voice(self) -> None:
        """Let the worker finish what the operator committed, then stop it.

        See :data:`VOICE_DRAIN_S`. ``_Stop`` goes on the end of the queue so
        everything before it — frames still waiting, the ``_End`` of a burst
        whose ``audioEnd`` arrived before the socket closed — is handled in
        order; the deadline is what keeps a closed socket from holding the
        connection task for the length of a model download.
        """
        worker = self._worker
        if worker is None:
            return
        if self._close_code != CLOSE_SERVICE_RESTART:
            self._voice.put(_Stop())
            with contextlib.suppress(TimeoutError, Exception):
                await asyncio.wait_for(asyncio.shield(worker), timeout=VOICE_DRAIN_S)
        if not worker.done():
            worker.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await worker

    async def _load_for(self, utterance: _Utterance) -> None:
        """Make sure the connection has a transcriber, loading it in a thread.

        A WORKING transcriber is cached for the connection and reused by every
        later burst — that is what the model load is paid for. A FAILING
        factory is deliberately not cached: it is retried on the next press,
        so an operator who installs the extra while the headset is still on
        gets voice back without reconnecting. The failure is reported on the
        burst that asked, unless that burst was already answered while the
        load ran — then the client has its error and this one would be noise.
        """
        self._discard_held()
        if self._transcriber is not None:
            return
        try:
            self._transcriber = await _in_thread(self._make_transcriber)
        except SpeechUnavailable as exc:
            await self._drop(utterance, "stt_unavailable", f"{exc.reason} — {exc.fix}")
        except Exception as exc:  # a broken factory must not cost the socket
            await self._drop(utterance, "stt_unavailable", _one_line(exc))

    async def _feed(self, utterance: _Utterance, chunk: bytes) -> None:
        """Feed this frame and every frame of the same burst waiting behind it.

        One ``feed`` per worker cycle, however many frames arrived while the
        last decode ran: :class:`~aisquare.services.xr.speech.BufferedTranscriber`
        decodes at most one interim per call, so a worker that fell a decode
        behind hands over the backlog as one chunk and is caught up, instead
        of paying one decode per frame for the rest of the press.
        """
        if utterance.dropped is not None:
            self._discard_held()
            return
        chunks = [chunk, *self._voice.take_frames_of(utterance)]
        transcriber = self._transcriber
        if transcriber is None:
            return  # the factory failed and this burst was answered with it
        self._held = utterance
        try:
            interim = await _in_thread(transcriber.feed, b"".join(chunks))
        except Exception as exc:
            await self._fail_burst(utterance, exc)
            return
        if utterance.dropped is not None:
            # A cap tripped, or a misaligned frame arrived, while the decode
            # ran: the client has its answer, and an interim after it would be
            # a stale repaint of a burst it has already abandoned.
            self._discard_held()
            return
        if interim:
            await self._send_frame(Stt(text=interim, final=False))

    async def _finish(self, utterance: _Utterance) -> None:
        """The final transcript, ROUTED FIRST, then ``stt`` and the ``ack`` on the wire.

        The client does not echo a ``prompt`` for voice, and that is the
        design rather than a shortcut: the operator committed when they
        released the trigger, and a round trip through the headset to ask them
        to confirm what they just said is exactly the latency §10 exists to
        remove. The interim frames are what let them see it coming.

        Routing before sending is the order that survives a client leaving:
        the sentence reaches the agent whether or not the headset is still
        there to be told, and the wire order the client sees — final ``stt``,
        then ``ack`` — is unchanged. Silence is not a prompt: an empty final
        goes out so the panel stops showing a live mic, and nothing is routed,
        because filing an empty board note against an operator who simply did
        not speak would be a write this server had no reason to make.
        """
        if utterance.dropped is not None:
            self._discard_held()
            return
        if utterance.audio_bytes == 0:
            # Counted at arrival, so it is known here, and zero is not a quiet
            # room — it is a capture graph that rendered nothing (a suspended
            # AudioContext, a tap released before the worklet's first render).
            # Answering that with the same empty final `stt` a silent press
            # gets left the operator re-pressing into a dead microphone with
            # nothing anywhere saying why, which is the outcome `_fail_burst`
            # calls the worst one. The code is the one this module used before
            # the transcriber was wired in, kept.
            await self._drop(
                utterance,
                "stt_empty",
                f"no audio arrived for the burst addressed to {utterance.session} — the "
                "microphone produced no frames between the header and audioEnd; check the "
                "capture graph and press again",
            )
            return
        transcriber = self._transcriber
        if transcriber is None:
            return  # answered with stt_unavailable when it was opened
        try:
            text = (await _in_thread(transcriber.finish)).strip()
        except Exception as exc:
            await self._fail_burst(utterance, exc)
            return
        self._held = None
        if not text:
            await self._send_frame(Stt(text="", final=True))
            return
        ack = await self._route(utterance.session, text)
        await self._send_frame(Stt(text=text, final=True))
        await self._send_frame(ack)

    async def _fail_burst(self, utterance: _Utterance, exc: Exception) -> None:
        """The backend raised: drop the burst AND the transcriber, keep the socket.

        ``stt_failed`` is the code this module already used for a backend that
        threw, kept rather than renamed. A decode that crashes is not one of
        the failure modes the voice contract enumerates, and answering it with
        silence would be the one outcome worse than any of them: a mic that
        looks live and produces nothing, with no line anywhere saying why.

        THE TRANSCRIBER IS DROPPED FOR CORRECTNESS, not for cost. A
        mid-utterance raise leaves it holding the buffer that caused it —
        :class:`~aisquare.services.xr.speech.BufferedTranscriber` resets only
        after a decode returns — so reusing it costs the operator voice for
        the rest of the connection rather than one sentence, and it surfaces
        as a socket that looks healthy and transcribes nothing. A fresh object
        is the only state this connection can reason about. The next press
        pays a model load for it, which is the right way round: the path that
        crashed pays, and the common one — press, speak, release, press again
        — keeps the model it loaded. ``tests/test_xr_server.py`` pins this
        with a poison sample: with the line below removed, a burst that made
        the backend raise kills the next burst too. (A dropped burst that did
        NOT crash — capped, misaligned, re-pressed — keeps the model and only
        discards its buffer; see :meth:`_discard_held`.)
        """
        self._transcriber = None
        self._held = None
        await self._drop(utterance, "stt_failed", _one_line(exc))

    def _discard_held(self) -> None:
        """Forget the audio the transcriber is holding for a burst that ended without a decode.

        Reached before the next burst's first frame and whenever the worker
        finds the burst it was feeding has been dropped. A discard is a buffer
        reset, not a decode and not a model reload: the abusive or unlucky
        burst — capped, misaligned, re-pressed — costs the operator nothing on
        the next press, where dropping the object instead would have cost a
        model load on the read loop.
        """
        if self._held is not None and self._transcriber is not None:
            self._transcriber.discard()
        self._held = None


# --- helpers --------------------------------------------------------------------


def _deliver(
    project: ProjectInfo, row: TeamSession, label: str | None, text: str
) -> tuple[bool, str]:
    """Blocking half of :meth:`_Connection._route`. Runs off the event loop.

    ``ok`` means the text REACHED THE AGENT, by either of the two routes the
    :class:`~aisquare.services.xr.protocol.Ack` docstring counts as delivery.
    :func:`services.fleet.tell` reports ``delivered=False`` whenever it filed
    a board note instead of typing — the agent was working, or its pane was
    not the agent yet — and passing that through as ``ok`` told the operator
    a prompt that was safely on the board was "not sent", while the no-pane
    path below reported the identical outcome as ok. Only an exception is a
    failure: ``tell`` raises when it could neither type nor file.
    """
    from aisquare.services import fleet as fleet_service
    from aisquare.services import team as team_service

    if label is not None:
        try:
            result = fleet_service.tell(project, label, text)
        except Exception as exc:
            return False, _one_line(exc)
        return True, result.how
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
    "CLOSE_TRY_AGAIN_LATER",
    "MAX_AUDIO_BYTES",
    "MAX_UTTERANCE_S",
    "POLL_MS",
    "VOICE_DRAIN_S",
    "TranscriberFactory",
    "build_app",
    "port_in_use",
    "run",
    "web_root",
)
