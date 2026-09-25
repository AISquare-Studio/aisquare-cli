"""The captain's voice IN: one page, one websocket, the transcriber, and two ways to talk.

``aisquare captain voice`` serves ``web/captain/index.html`` and ``/ws`` on
localhost. The page opens the microphone (a browser grants it only in a secure
context: ``http://localhost`` is one, a LAN address is not — which is why the
phone path is ``adb reverse`` over USB tonight), ships 16 kHz mono PCM16LE in
20 ms frames as binary websocket messages, and hears back JSON: interim and
final transcripts, what was delivered, whether the captain is thinking, and
the reply. Every final transcript goes to :func:`brain.say` — the one delivery
T2 owns (contract 13136): typed into the captain's pane, the reply read back
from its transcript — and the reply is spoken through the Speaker (T3's
:mod:`speaker`). The same JSON events carry a typed request too, so a later
text or Slack adapter needs no voice code.

TWO MODES, on the page and on the CLI. ``focus`` is push-to-talk: the client
sends an ``audio`` header while the button (or the space bar) is held, frames,
then ``audioEnd``; the whole burst is one utterance. ``listen`` keeps the mic
open: the client streams frames without headers and the SERVER cuts them into
utterances — the RMS gate opens on speech and :data:`SILENCE_MS` of quiet ends
one (:class:`Segmenter`) — with a mute toggle on the page and a stop word that
turns the mic off instead of being delivered.

THE TRANSCRIBER is lifted from cliXR (``services/xr/speech.py`` at 2723819)
whole: :class:`BufferedTranscriber` over any ``decode`` callable — the RMS gate,
the buffer, the interim cadence over a bounded window — with faster-whisper
imported LAZILY inside the factory (this package runs on the Claude Code hook
path, where the import would cost hundreds of milliseconds and is not
installed under the plain dev extra), and :class:`FakeTranscriber` so the whole
path is testable without a model.

THE THINKING SIGNAL: the page shows *thinking* while a delivery is in flight,
while T1's busy flag (``thinking on``) is set, or while the captain's pane
reads ``working``, and the CLI's terminal shows the same through a hook; if a
reply takes longer than :data:`CUE_AFTER_S` the Speaker says one short cue
("on it"). THE BRAIN DECIDES WHAT IS WORTH SAYING (plan section 2, manager seq
13143): the reply text is spoken only when the captain made no ``speak()`` call
during that turn — read from the ``captain_action`` audit on the home board —
so a captain that forgets still answers audibly and no answer is heard twice.
The speech spool itself is drained by ONE thread in the captain's server
process (``speaker.start_drainer``, started by ``actions.run_stdio``), not here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from array import array
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import sqrt
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from aisquare.core.version import DISTRIBUTION
from aisquare.services import fleet as fleet_service
from aisquare.services.captain import brain
from aisquare.services.captain import speaker as speaker_mod
from aisquare.services.captain import state as captain_state

if TYPE_CHECKING:
    from starlette.applications import Starlette
    from starlette.websockets import WebSocket

log = logging.getLogger(__name__)

# --- the wire: 16 kHz mono PCM16LE in 20 ms frames — cliXR's contract, stated once here -----------

SAMPLE_RATE = 16_000
SAMPLE_BYTES = 2
CHANNELS = 1
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * SAMPLE_BYTES * CHANNELS * FRAME_MS // 1000
BYTES_PER_SECOND = SAMPLE_RATE * SAMPLE_BYTES * CHANNELS
#: How much buffered speech earns an interim decode, and how much of the buffer's tail an
#: interim re-decodes (whisper has no streaming API): cliXR's measured numbers.
INTERIM_SECONDS = 1.0
INTERIM_BYTES = int(BYTES_PER_SECOND * INTERIM_SECONDS)
INTERIM_WINDOW_SECONDS = 4.0
INTERIM_WINDOW_BYTES = int(BYTES_PER_SECOND * INTERIM_WINDOW_SECONDS)
#: RMS below which a frame is room tone: 16-bit speech at a headset mic sits in the
#: thousands, a quiet room in the low hundreds.
SILENCE_RMS = 350.0
#: Always-listening: this much trailing quiet, once speech began, ends the utterance.
SILENCE_MS = 900
SILENCE_BYTES = BYTES_PER_SECOND * SILENCE_MS // 1000
#: The longest utterance either mode accepts; past it the burst is finished as it stands.
MAX_UTTERANCE_S = 60.0
MAX_AUDIO_BYTES = int(BYTES_PER_SECOND * MAX_UTTERANCE_S)
#: What turns the mic off in always-listening mode instead of being delivered (matched on
#: the whole final transcript, case and trailing punctuation aside).
STOP_WORDS: frozenset[str] = frozenset({"stop listening"})
WAKE_WORD = "captain"
"""Listen mode's wake word (the owner's request, 13284): only an utterance that begins with it
is delivered, stripped of it; ``[captain] wake_word`` in config.toml changes it, ``''`` turns
it off. Focus mode (hold to talk) never needs it."""
WAKE_WINDOW_S = 5.0
"""How long the wake word ALONE keeps the mic open for the next utterance, delivered bare."""
#: A reply slower than this earns one spoken cue, so the owner knows the request landed.
CUE_AFTER_S = 3.0
CUE_TEXT = "on it"
#: How often the thinking signal and the mode key are looked at.
POLL_S = 1.0
MODE_STATE_KEY = "captain_voice_mode"
"""state.json: the mode's single home (13179) — a plain ``"focus"`` or ``"listen"``. The
page's toggle, the CLI's ``--mode`` and T4's TUI control all write it; every connected page
follows it within :data:`POLL_S`."""
AUTH_TIMEOUT_S = 5.0
CLOSE_AUTH_FAILED = 4401
CLOSE_AUTH_TIMEOUT = 4408
DEFAULT_PORT = 8749
"""``serve`` owns 8747 and cliXR's ``xr`` 8748: neighbours."""
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})
REQUIRED_MODULES = ("starlette", "uvicorn", "websockets")
"""What the server imports at serve time; probed by the CLI so a missing extra is a sentence."""
DEFAULT_MODEL = "base.en"
ALLOWED_MODELS = ("base.en", "small.en")
INSTALL_FIX = f"pip install '{DISTRIBUTION}[voice]'"

Mode = Literal["focus", "listen"]
MODES: tuple[Mode, ...] = ("focus", "listen")


# --- the transcriber, lifted from cliXR -----------------------------------------------------------


class SpeechUnavailable(RuntimeError):
    """Voice cannot start, with the fix attached — ``reason`` and ``fix`` ride the wire apart."""

    def __init__(self, reason: str, fix: str) -> None:
        super().__init__(f"{reason} — {fix}")
        self.reason = reason
        self.fix = fix


@runtime_checkable
class Transcriber(Protocol):
    """Audio in, text out — the whole contract the server codes against."""

    def feed(self, pcm: bytes) -> str | None:
        """Take one chunk of 16 kHz mono PCM16LE; interim text when there is something new."""

    def finish(self) -> str:
        """End the utterance and return the final transcript (``""`` if silent)."""

    def discard(self) -> None:
        """Forget the utterance so far without decoding it."""


def rms(pcm: bytes) -> float:
    """Root-mean-square level of a PCM16LE chunk; 0.0 for an empty one. A trailing odd byte
    is ignored (a level meter), and the samples are read little-endian on every machine."""
    import sys

    usable = len(pcm) - (len(pcm) % SAMPLE_BYTES)
    if usable <= 0:
        return 0.0
    samples = array("h")
    samples.frombytes(pcm[:usable])
    if sys.byteorder == "big":
        samples.byteswap()
    return sqrt(sum(sample * sample for sample in samples) / len(samples))


class BufferedTranscriber:
    """The gate, the buffer and the interim cadence over any ``decode`` (cliXR, unchanged).

    The gate is one-way: frames are dropped until one is loud enough to be
    speech, then everything is kept, silence included. A chunk that is not
    sample-aligned is refused with ``ValueError`` at the chunk that caused it.
    An interim decodes a bounded trailing window; ``finish`` decodes the whole
    utterance. Reusable across utterances, so one per connection pays the
    model load once.
    """

    def __init__(
        self,
        decode: Callable[[bytes], str],
        *,
        silence_rms: float = SILENCE_RMS,
        interim_bytes: int = INTERIM_BYTES,
        window_bytes: int = INTERIM_WINDOW_BYTES,
    ) -> None:
        self._decode = decode
        self._silence_rms = silence_rms
        self._interim_bytes = interim_bytes
        self._window_bytes = window_bytes
        self._buffer = bytearray()
        self._speaking = False
        self._decoded_at = 0
        self._last = ""

    def feed(self, pcm: bytes) -> str | None:
        if not pcm:
            return None
        if len(pcm) % SAMPLE_BYTES:
            raise ValueError(
                f"a {len(pcm)}-byte chunk is not a whole number of {SAMPLE_BYTES}-byte samples"
            )
        if not self._speaking:
            if rms(pcm) < self._silence_rms:
                return None
            self._speaking = True
        self._buffer.extend(pcm)
        if len(self._buffer) - self._decoded_at < self._interim_bytes:
            return None
        self._decoded_at = len(self._buffer)
        start = len(self._buffer) - min(len(self._buffer), self._window_bytes)
        text = self._decode(bytes(self._buffer[start:])).strip()
        if not text or text == self._last:
            return None
        self._last = text
        return text

    def finish(self) -> str:
        if not self._buffer:
            self._reset()
            return ""
        text = self._decode(bytes(self._buffer)).strip()
        self._reset()
        return text

    def discard(self) -> None:
        self._reset()

    def _reset(self) -> None:
        self._buffer = bytearray()
        self._speaking = False
        self._decoded_at = 0
        self._last = ""


class FakeTranscriber:
    """A :class:`Transcriber` that answers canned text and records what it was fed.

    ``canned`` may be one string, said for every utterance, or a list said in
    order (the always-listening test needs two different sentences). ``fed``
    keeps every frame, ``finished`` counts the utterances.
    """

    def __init__(self, canned: str | list[str] = "", *, interim_bytes: int = INTERIM_BYTES) -> None:
        self._canned = [canned] if isinstance(canned, str) else list(canned)
        self.fed = bytearray()
        self.finished = 0
        self.discarded = 0
        self._interim_bytes = interim_bytes
        self._emitted = False
        self._since = 0

    @property
    def canned(self) -> str:
        """The text the NEXT utterance ends with (the last one repeats)."""
        if not self._canned:
            return ""
        return self._canned[min(self.finished, len(self._canned) - 1)]

    def feed(self, pcm: bytes) -> str | None:
        self.fed.extend(pcm)
        self._since += len(pcm)
        if self._emitted or not self.canned or self._since < self._interim_bytes:
            return None
        self._emitted = True
        return self.canned

    def finish(self) -> str:
        text = self.canned
        self.finished += 1
        self._emitted = False
        self._since = 0
        return text

    def discard(self) -> None:
        self.discarded += 1
        self._emitted = False
        self._since = 0


def _whisper_decode(model: str) -> Callable[[bytes], str]:
    """Load faster-whisper and return a ``bytes -> text`` decoder: THE ONLY import of it."""
    try:
        import numpy
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise SpeechUnavailable(f"faster-whisper is not installed ({exc})", INSTALL_FIX) from exc
    try:
        whisper = WhisperModel(model, device="cpu", compute_type="int8")
    except Exception as exc:
        raise SpeechUnavailable(
            f"the whisper model {model!r} could not be loaded ({exc})",
            f'Pre-download it once: python -c "from faster_whisper import WhisperModel; '
            f"WhisperModel('{model}', device='cpu', compute_type='int8')\"",
        ) from exc

    def decode(pcm: bytes) -> str:
        samples = numpy.frombuffer(pcm, dtype=numpy.int16).astype(numpy.float32) / 32768.0
        segments, _info = whisper.transcribe(
            samples,
            language="en",
            beam_size=1,
            condition_on_previous_text=False,
            # The RMS gate opens on any loud frame; the VAD decides whether there
            # was speech, or whisper hallucinates "Thank you." over room tone.
            vad_filter=True,
        )
        return " ".join(str(segment.text).strip() for segment in segments).strip()

    return decode


def transcriber(model: str | None = None) -> Transcriber:
    """A live :class:`Transcriber` on CPU; raises :class:`SpeechUnavailable` with the fix."""
    name = (model or "").strip() or DEFAULT_MODEL
    if name not in ALLOWED_MODELS:
        raise SpeechUnavailable(
            f"{name!r} is not a supported model for voice prompts",
            f"voice input is command input, where latency dominates: use one of "
            f"{', '.join(ALLOWED_MODELS)}",
        )
    return BufferedTranscriber(_whisper_decode(name))


# --- always-listening: the server cuts the stream into utterances ---------------------------------


class Segmenter:
    """Frames in, utterances out: the RMS gate opens one, trailing quiet closes it.

    Room tone before speech is dropped (the transcriber's own gate would drop
    it too); once a frame is loud, every frame is fed until :data:`SILENCE_MS`
    of quiet has passed or the utterance hits :data:`MAX_UTTERANCE_S`, and the
    transcriber's ``finish`` is the final. ``flush`` ends an utterance early —
    the mic muted, the mode switched — so nothing spoken is lost.
    """

    def __init__(
        self,
        transcriber: Transcriber,
        *,
        silence_rms: float = SILENCE_RMS,
        silence_bytes: int = SILENCE_BYTES,
        max_bytes: int = MAX_AUDIO_BYTES,
    ) -> None:
        self._transcriber = transcriber
        self._silence_rms = silence_rms
        self._silence_bytes = silence_bytes
        self._max_bytes = max_bytes
        self.speaking = False
        self._quiet = 0
        self._bytes = 0

    def feed(self, pcm: bytes) -> tuple[str | None, str | None]:
        """Feed one frame; returns ``(interim, final)``, each ``None`` when there is none."""
        loud = rms(pcm) >= self._silence_rms
        if not self.speaking:
            if not loud:
                return None, None
            self.speaking = True
        interim = self._transcriber.feed(pcm)
        self._bytes += len(pcm)
        self._quiet = 0 if loud else self._quiet + len(pcm)
        if self._quiet >= self._silence_bytes or self._bytes >= self._max_bytes:
            return interim, self._end()
        return interim, None

    def flush(self) -> str | None:
        """End the open utterance now, if there is one."""
        return self._end() if self.speaking else None

    def _end(self) -> str:
        self.speaking = False
        self._quiet = 0
        self._bytes = 0
        return self._transcriber.finish()


WAKE_WORD_SHAPE = re.compile(r"[a-z]+(?: [a-z]+)*")


def configured_wake_word(config_path: Path | None = None) -> str:
    """``[captain] wake_word`` from config.toml: ``''`` switches it off; absent, the default.

    A to z letters and spaces, one word or a few ('captain', 'hey captain'). Anything else
    raises ValueError, which the CLI refuses in one line like a bad speaker name: a typo
    must never read as 'off', since the owner's meetings must never reach the captain. A
    file that does not parse is said in the log and reads as the default, as for the speaker.
    """
    value = speaker_mod.captain_table(config_path).get("wake_word")
    if value is None:
        return WAKE_WORD
    shape = "a to z letters and spaces, e.g. \"captain\"; '' switches it off"
    if not isinstance(value, str):
        raise ValueError(f"[captain] wake_word = {value!r} in config.toml is not text: {shape}")
    word = " ".join(value.lower().split())
    if word and not WAKE_WORD_SHAPE.fullmatch(word):
        raise ValueError(f"[captain] wake_word = {value!r} in config.toml: {shape}")
    return word


_NOT_LETTERS = re.compile(r"[^a-z]+")


def _one_edit_apart(a: str, b: str) -> bool:
    """Whether ``a`` and ``b`` differ by one substitution, insertion, deletion or swap — whisper's
    near-spellings of a word ('kaptain', 'captian', 'captin')."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diffs = [index for index, (x, y) in enumerate(zip(a, b, strict=True)) if x != y]
        if len(diffs) == 1:
            return True
        return (
            len(diffs) == 2
            and diffs[1] == diffs[0] + 1
            and a[diffs[0]] == b[diffs[1]]
            and a[diffs[1]] == b[diffs[0]]
        )
    short, long = (a, b) if len(a) < len(b) else (b, a)
    return any(long[:index] + long[index + 1 :] == short for index in range(len(long)))


def _near(token: str, expected: str) -> bool:
    """One word of the wake word, as whisper may spell it: exact under four letters, else one
    edit away — but never the plural or the possessive ('captains', "captain's"), which are
    other words a meeting says."""
    if token == expected:
        return True
    if len(expected) < 4 or token == expected + "s":
        return False
    return _one_edit_apart(token, expected)


class WakeWord:
    """The wake word's match on a final transcript: (woke, the rest to deliver)."""

    def __init__(self, word: str) -> None:
        self.words = tuple(re.findall(r"[a-z]+", word.lower()))
        self.word = " ".join(self.words)

    def __bool__(self) -> bool:
        return bool(self.words)

    def match(self, text: str) -> tuple[bool, str]:
        """Whether ``text`` begins with the wake word, each of its words forgiven case,
        punctuation and one misspelling (:func:`_near`), and what follows it, stripped."""
        if not self.words:
            return False, ""
        rest = text
        for expected in self.words:
            head = re.match(r"^[\W_]*([A-Za-z']+)(.*)$", rest, re.S)
            if head is None or not _near(_NOT_LETTERS.sub("", head.group(1).lower()), expected):
                return False, ""
            rest = head.group(2)
        return True, re.sub(r"^[\W_]+", "", rest).strip()


def is_stop_word(text: str) -> bool:
    """Whether a final transcript is the stop word, case and trailing punctuation aside."""
    return " ".join(text.lower().split()).strip(" .!?,") in STOP_WORDS


# --- the seams ------------------------------------------------------------------------------------


class DeliveryFailed(RuntimeError):
    """The captain did not answer: the message is what the page and the Speaker say."""


NO_TEXT_NOTE = "the captain answered with tools alone — its pane shows what it did"
"""Shown on the page for a turn that ended without text (contract 13175); never spoken."""


@dataclass(frozen=True)
class Delivered:
    """What a delivery came back with: the reply text (``None`` for a turn of tools alone,
    13175) and when the text went into the pane — the moment the speak() window opens."""

    text: str | None
    typed_at: datetime | None = None


def deliver_to_captain(text: str) -> Delivered:
    """The product delivery: T2's ``brain.say`` — typed into the pane, the reply read back.

    A ``NoReply`` and a fleet refusal (T2's ``brain.Unreachable`` after a reboot is
    one) are both a failed delivery the page says; nothing else is expected, and
    anything else is caught by the turn and said as ``internal``.
    """
    try:
        reply = brain.say(text)
    except (brain.NoReply, fleet_service.FleetError) as exc:
        raise DeliveryFailed(str(exc)) from exc
    return Delivered(reply.text, typed_at=reply.typed_at)


def voice_mode() -> Mode | None:
    """The mode in state.json (:data:`MODE_STATE_KEY`), or ``None`` when it was never set —
    anything else written there is said in the log and read as unset, never as a mode."""
    from aisquare.core import state_file

    raw = state_file.read_state().get(MODE_STATE_KEY)
    if raw is None:
        return None
    if raw in MODES:
        return "listen" if raw == "listen" else "focus"
    log.warning(
        "captain voice: state.json %s=%r is not one of %s; ignored", MODE_STATE_KEY, raw, MODES
    )
    return None


def set_voice_mode(mode: Mode) -> None:
    """Write the mode (the page's toggle, ``--mode``, T4's control): connected pages follow."""
    from aisquare.core import state_file

    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
    state_file.update_state(MODE_STATE_KEY, mode)


def captain_is_thinking() -> bool:
    """T1's busy flag, or a captain pane that reads ``working``."""
    if captain_state.busy_since() is not None:
        return True
    agent = brain.find()
    return agent is not None and fleet_service.status_of(agent).state == "working"


def home_seq() -> int:
    """The home board's latest event seq: where a turn starts, for :func:`spoke_since`."""
    from aisquare.core.store import store_session

    with store_session() as store:
        return store.latest_seq(captain_state.home_project().id)


def spoke_since(seq: int, typed_at: datetime | None = None) -> int:
    """How many ``speak()`` calls the captain audited on the home board past ``seq`` — and,
    when ``typed_at`` is known, not before the text went in (coderp's S3: a speak() from the
    busy turn ``say`` waited out, or from another page's turn, is not this turn's)."""
    from aisquare.core.store import store_session

    home = captain_state.home_project()
    with store_session() as store:
        events = store.filtered_events(home.id, since_seq=seq, kind="captain_action", limit=500)
    spoken = 0
    for event in events:
        try:
            record = json.loads(event.text)
        except ValueError:
            continue
        if not (isinstance(record, dict) and record.get("tool") == "speak" and record.get("ok")):
            continue
        if typed_at is not None and _event_time(event.created_at) < typed_at:
            continue
        spoken += 1
    return spoken


def _event_time(raw: object) -> datetime:
    """An event's ``created_at`` as an aware datetime."""
    if isinstance(raw, datetime):
        stamp = raw
    else:
        try:
            stamp = datetime.fromisoformat(str(raw))
        except ValueError:
            return datetime.min.replace(tzinfo=UTC)
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


@dataclass
class Hooks:
    """Everything the server reaches outside itself, replaceable in one place for a test."""

    transcriber_factory: Callable[[], Transcriber] = transcriber
    deliver: Callable[[str], Delivered] = deliver_to_captain
    voice: speaker_mod.Voice = field(default_factory=speaker_mod.machine_voice)
    thinking: Callable[[], bool] = captain_is_thinking
    voice_mode: Callable[[], Mode | None] = voice_mode
    set_voice_mode: Callable[[Mode], None] = set_voice_mode
    home_seq: Callable[[], int] = home_seq
    spoke_since: Callable[[int, datetime | None], int] = spoke_since
    on_thinking: Callable[[bool], None] | None = None
    """The CLI's side of the thinking signal: called on every flip, the terminal prints it."""
    clock: Callable[[], float] = time.monotonic
    cue_after_s: float = CUE_AFTER_S
    poll_s: float = POLL_S
    # Late-bound, so the config is read as the server starts; the CLI reads it first, to
    # refuse a bad value in one line (configured_wake_word raises).
    wake_word: str = field(default_factory=lambda: configured_wake_word())
    wake_window_s: float = WAKE_WINDOW_S


# --- the page -------------------------------------------------------------------------------------


def page_bytes() -> bytes:
    """``web/captain/index.html`` out of the installed package (a wheel may be zipped)."""
    from importlib.resources import files

    return (files("aisquare.web.captain") / "index.html").read_bytes()


def voice_url(port: int, token: str) -> str:
    """The URL to open: ``localhost`` (the secure origin), the token in the FRAGMENT so no
    server log or referrer ever sees it — cliXR's idiom."""
    return f"http://localhost:{port}/#token={token}"


def adb_reverse(port: int) -> str:
    """The one line that makes an Android phone's ``localhost:<port>`` this machine's."""
    return f"adb reverse tcp:{port} tcp:{port}"


def qr_lines(url: str) -> list[str] | None:
    """The URL as a QR in terminal characters, or ``None`` when ``segno`` is not installed."""
    try:
        import segno
    except ImportError:
        return None
    code = segno.make(url, error="m")
    matrix: list[list[int]] = [list(row) for row in code.matrix]
    lines: list[str] = []
    padded = [*matrix[1::2], [0] * len(matrix[0])]
    for top, bottom in zip(matrix[0::2], padded, strict=False):
        lines.append(
            "".join(
                "█" if up and down else "▀" if up else "▄" if down else " "
                for up, down in zip(top, bottom, strict=False)
            )
        )
    return lines


# --- the server -----------------------------------------------------------------------------------


def _frame(kind: str, **fields: object) -> str:
    return json.dumps({"t": kind, **fields}, ensure_ascii=False)


class _Connection:
    """One browser, one websocket: auth, then the read loop with its two side tasks."""

    def __init__(self, websocket: WebSocket, *, token: str, hooks: Hooks, mode: Mode) -> None:
        self._ws = websocket
        self._token = token
        self._hooks = hooks
        # The key is the mode's single home (13179): it wins over the server's default.
        self.mode: Mode = self._mode_key() or mode
        self.listening = self.mode == "listen"
        self._transcriber: Transcriber | None = None
        self._segmenter: Segmenter | None = None
        self._in_burst = False
        self._burst_bytes = 0
        self._delivering = 0
        self._deliveries = asyncio.Lock()
        self._turns: set[asyncio.Task[None]] = set()
        self._mode_writing = 0
        self._wake = WakeWord(hooks.wake_word)
        self._awake_until: float | None = None
        """When the window the wake word alone opened closes (``hooks.clock``), or None."""
        self._window_timer: asyncio.Task[None] | None = None
        self._shown_thinking: bool | None = None
        self._send_lock = asyncio.Lock()

    # -- lifecycle

    async def serve(self) -> None:
        await self._ws.accept()
        if not await self._authenticate():
            return
        self._shown_thinking = self._thinking_now()
        await self._send(
            "hello",
            mode=self.mode,
            listening=self.listening,
            speaker=speaker_mod.speaker_on(),
            thinking=self._shown_thinking,
            wake_word=self._wake.word,
        )
        poller = asyncio.create_task(self._thinking_loop())
        try:
            await self._read_loop()
        finally:
            poller.cancel()
            with _swallow_cancel():
                await poller
            timer = self._window_timer
            self._cancel_window_timer()
            if timer is not None:
                with _swallow_cancel():
                    await timer
            # A turn in flight is AWAITED, not cancelled: the owner who closed the tab is
            # still in the room, and the reply is still theirs to hear. Its frames go to
            # a socket that is gone, which _send swallows.
            for turn in list(self._turns):
                with _swallow_cancel():
                    await turn

    async def _authenticate(self) -> bool:
        from starlette.websockets import WebSocketDisconnect

        try:
            packet = await asyncio.wait_for(self._ws.receive(), AUTH_TIMEOUT_S)
        except TimeoutError:
            await self._reject(CLOSE_AUTH_TIMEOUT, "auth_timeout", "no auth frame arrived in time")
            return False
        except (WebSocketDisconnect, RuntimeError):
            return False
        text = packet.get("text")
        if packet.get("type") == "websocket.disconnect" or not isinstance(text, str):
            await self._reject(CLOSE_AUTH_TIMEOUT, "auth_invalid", "the first frame must be auth")
            return False
        try:
            message = json.loads(text)
        except ValueError:
            message = None
        if not isinstance(message, dict) or message.get("t") != "auth":
            await self._reject(CLOSE_AUTH_TIMEOUT, "auth_invalid", "the first frame must be auth")
            return False
        import secrets  # here, not at module scope: the hook path's import ratchet

        supplied = str(message.get("token", "")).encode("utf-8", "surrogatepass")
        if not secrets.compare_digest(supplied, self._token.encode("utf-8", "surrogatepass")):
            await self._reject(CLOSE_AUTH_FAILED, "auth_failed", "the token was rejected")
            return False
        return True

    async def _reject(self, close: int, code: str, message: str) -> None:
        with _swallow_socket_errors():
            await self._ws.send_text(_frame("error", code=code, message=message))
            await self._ws.close(code=close)

    async def _send(self, kind: str, **fields: object) -> None:
        async with self._send_lock:
            with _swallow_socket_errors():
                await self._ws.send_text(_frame(kind, **fields))

    # -- inbound

    async def _read_loop(self) -> None:
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
                try:
                    await self._on_audio(data)
                except Exception as exc:  # one bad frame must not kill the socket
                    log.warning("captain voice: a frame failed: %s", exc)
                    await self._send("error", code="audio_failed", message=str(exc))
                continue
            text = packet.get("text")
            if text is None:
                continue
            try:
                message = json.loads(text)
            except ValueError:
                await self._send("error", code="bad_message", message="not JSON")
                continue
            if not isinstance(message, dict):
                await self._send("error", code="bad_message", message="not an object")
                continue
            try:
                await self._dispatch(message)
            except Exception as exc:  # one bad request must not kill the socket
                log.warning("captain voice: %s failed: %s", message.get("t"), exc)
                await self._send("error", code="internal", message=str(exc))

    async def _dispatch(self, message: dict[str, Any]) -> None:
        kind = message.get("t")
        if kind == "auth":
            return  # a reconnecting client replays it; not an error
        if kind == "mode":
            await self._set_mode(str(message.get("mode", "")))
        elif kind == "audio":
            await self._open_burst()
        elif kind == "audioEnd":
            await self._close_burst()
        elif kind == "listen":
            await self._set_listening(bool(message.get("on", True)))
        elif kind == "stop":
            await self._set_listening(False)
        elif kind == "text":
            text = str(message.get("text", "")).strip()
            if text and self.mode == "listen" and is_stop_word(text):
                await self._heard_stop_word()  # typed, the stop word is the stop word
            elif text:
                self._start_delivery(text)
        elif kind == "speaker":
            speaker_mod.set_speaker(bool(message.get("on", True)))
            await self._send("speaker", on=speaker_mod.speaker_on())
        else:
            await self._send("error", code="bad_message", message=f"unknown frame {kind!r}")

    async def _set_mode(self, mode: str, *, write: bool = True) -> None:
        if mode not in MODES:
            await self._send("error", code="bad_message", message=f"mode must be one of {MODES}")
            return
        await self._flush_listen()
        if self._in_burst:
            await self._close_burst()
        self.mode = "listen" if mode == "listen" else "focus"
        self.listening = self.mode == "listen"
        if self._awake_until is not None:
            await self._close_window()
        if write:
            # The page's toggle writes the key (13179), so the TUI and every other
            # page agree; a switch that CAME from the key is not written back. While
            # the write is in flight the poll still reads the old value: it must not
            # bounce the page (coderp's minor), so it stands aside until the write lands.
            self._mode_writing += 1
            try:
                await asyncio.to_thread(self._hooks.set_voice_mode, self.mode)
            except Exception as exc:  # the page still switched; the key did not, said
                log.warning("captain voice: the mode key could not be written: %s", exc)
                await self._send(
                    "error", code="mode_not_saved", message=f"the mode was not saved: {exc}"
                )
            finally:
                self._mode_writing -= 1
        await self._send("mode", mode=self.mode, listening=self.listening)

    def _mode_key(self) -> Mode | None:
        try:
            return self._hooks.voice_mode()
        except Exception as exc:  # a courtesy read: the page keeps its mode, said
            log.warning("captain voice: the mode key could not be read: %s", exc)
            return None

    async def _follow_mode_key(self) -> None:
        """The one-second poll's other job (13179): a key another writer changed — T4's
        control, another page, ``--mode`` — switches this page, and the page is told."""
        if self._mode_writing:
            return
        wanted = await asyncio.to_thread(self._mode_key)
        if wanted is not None and wanted != self.mode and not self._mode_writing:
            await self._set_mode(wanted, write=False)

    async def _set_listening(self, on: bool) -> None:
        if not on:
            await self._flush_listen()
        self.listening = on and self.mode == "listen"
        if not self.listening and self._awake_until is not None:
            await self._close_window()
        await self._send("listening", on=self.listening)

    async def _flush_listen(self) -> None:
        if self._segmenter is not None and self._segmenter.speaking:
            final = await asyncio.to_thread(self._segmenter.flush)
            await self._final(final or "")

    # -- audio

    def _ensure_transcriber(self) -> Transcriber:
        if self._transcriber is None:
            self._transcriber = self._hooks.transcriber_factory()
        return self._transcriber

    async def _open_burst(self) -> None:
        if self.mode != "focus":
            await self._send(
                "error", code="bad_message", message="audio headers belong to focus mode"
            )
            return
        if self._in_burst:
            await self._close_burst()  # a re-press: the open burst is committed, not lost
        self._in_burst = True
        self._burst_bytes = 0
        try:
            self._ensure_transcriber()
        except SpeechUnavailable as exc:
            self._in_burst = False
            await self._send("error", code="stt_unavailable", message=exc.reason, fix=exc.fix)

    async def _close_burst(self) -> None:
        if not self._in_burst:
            return
        self._in_burst = False
        transcriber = self._ensure_transcriber()
        final = await asyncio.to_thread(transcriber.finish)
        await self._final(final)

    async def _on_audio(self, chunk: bytes) -> None:
        if len(chunk) % SAMPLE_BYTES:
            raise ValueError(f"a {len(chunk)}-byte frame is not a whole number of samples")
        if self.mode == "focus":
            if not self._in_burst:
                return  # a frame after audioEnd: the worklet's tail, ignored
            self._burst_bytes += len(chunk)
            transcriber = self._ensure_transcriber()
            interim = await asyncio.to_thread(transcriber.feed, chunk)
            if interim:
                await self._send("stt", text=interim, final=False)
            if self._burst_bytes >= MAX_AUDIO_BYTES:
                await self._close_burst()
            return
        if not self.listening:
            return
        if self._segmenter is None:
            try:
                self._segmenter = Segmenter(self._ensure_transcriber())
            except SpeechUnavailable as exc:
                self.listening = False
                await self._send("error", code="stt_unavailable", message=exc.reason, fix=exc.fix)
                await self._send("listening", on=False)
                return
        interim, final = await asyncio.to_thread(self._segmenter.feed, chunk)
        if interim:
            await self._send("stt", text=interim, final=False)
        if final is not None:
            await self._final(final)

    async def _final(self, text: str) -> None:
        """A finished utterance: shown, then delivered — or, as the stop word, the mic off.

        In listen mode the wake word, when set, decides first (:meth:`_gate`). The bare
        stop word goes before it: the safety never needs the wake word.
        """
        text = text.strip()
        if text and self.mode == "listen" and is_stop_word(text):
            await self._send("stt", text=text, final=True)
            await self._heard_stop_word()
            return
        if text and self.mode == "listen" and self._wake:
            await self._gate(text)
            return
        await self._send("stt", text=text, final=True)
        if text:
            self._start_delivery(text)

    async def _gate(self, text: str) -> None:
        """Listen mode's wake word (13284). An utterance that begins with it is delivered
        stripped of it; the word alone opens a short window in which the next utterance
        goes through bare; anything else is dropped — never delivered, never spoken, and
        not kept: its text never leaves this function, the page only clears its interim.
        """
        woke, rest = self._wake.match(text)
        in_window = self._awake_until is not None and self._hooks.clock() < self._awake_until
        if not woke and not in_window:
            if self._awake_until is not None:
                await self._close_window()
            await self._send("stt", text="", final=True, dropped="no wake word")
            return
        await self._send("stt", text=text, final=True)
        if woke and not rest:
            await self._open_window()
            return
        if self._awake_until is not None:
            await self._close_window()
        request = rest if woke else text
        if is_stop_word(request):  # "Captain, stop listening" is the stop word too
            await self._heard_stop_word()
            return
        self._start_delivery(request)

    async def _heard_stop_word(self) -> None:
        """The stop word, spoken or typed: the mic goes off, and an open window with it — a
        window that outlived the mute would deliver the first utterance after an unmute."""
        self.listening = False
        if self._awake_until is not None:
            await self._close_window()
        await self._send("listening", on=False, why="stop word")

    async def _open_window(self) -> None:
        """The wake word alone: the next utterance goes through bare for ``wake_window_s``.

        The ``awake`` frame is the cue: the page shows 'listening' and plays a short tone.
        Nothing is spoken here — the machine's voice would reach the mic inside the window
        and be delivered as the request; the page's own tone is taken out of the mic.
        """
        self._awake_until = self._hooks.clock() + self._hooks.wake_window_s
        self._cancel_window_timer()
        self._window_timer = asyncio.create_task(self._close_window_when_due())
        await self._send("awake", on=True, seconds=self._hooks.wake_window_s)

    async def _close_window(self) -> None:
        self._awake_until = None
        self._cancel_window_timer()
        await self._send("awake", on=False)

    async def _close_window_when_due(self) -> None:
        """Silence after the wake word: the window closes on its own, and the page's
        'listening' goes back to 'say Captain'."""
        while self._awake_until is not None:
            remaining = self._awake_until - self._hooks.clock()
            if remaining <= 0:
                break
            await asyncio.sleep(remaining)
        if self._awake_until is not None:
            self._awake_until = None
            self._window_timer = None
            await self._send("awake", on=False)

    def _cancel_window_timer(self) -> None:
        timer, self._window_timer = self._window_timer, None
        if timer is not None:
            timer.cancel()

    def _start_delivery(self, text: str) -> None:
        """A turn runs as its own task, never inside the read loop (coderp's B1): with the
        loop blocked, uvicorn stopped reading the page's pongs and its keepalive closed the
        socket 20 to 40 s into any longer turn, and the reply went to a dead socket. The
        turns of one page still run one at a time (``_deliveries``); the read loop keeps
        receiving, so a mute, a stop, a mode switch or the speaker switch lands at once."""
        task = asyncio.create_task(self._deliver(text))
        self._turns.add(task)
        task.add_done_callback(self._turns.discard)

    async def _deliver(self, text: str) -> None:
        await self._send("utterance", text=text)
        async with self._deliveries:
            self._delivering += 1
            await self._show_thinking()
            since = await asyncio.to_thread(self._read_home_seq)
            cue = asyncio.create_task(self._cue_later())
            ended = False
            try:
                try:
                    delivered = await asyncio.to_thread(self._hooks.deliver, text)
                except DeliveryFailed as exc:
                    cue.cancel()
                    await self._send("error", code="no_reply", message=str(exc))
                    ended = await self._end_turn()
                    await asyncio.to_thread(self._hooks.voice.utter, "the captain did not answer")
                    return
                except Exception as exc:  # said, never a page stuck on "thinking" (S2)
                    cue.cancel()
                    log.warning("captain voice: the delivery failed: %s", exc)
                    await self._send("error", code="internal", message=str(exc))
                    ended = await self._end_turn()
                    return
                cue.cancel()
                # The brain decides what is worth saying (13143): a turn in which the
                # captain called speak() is already audible; only a silent turn's reply
                # is spoken here, so nothing is heard twice and nothing is missed. The
                # window opens when the text went in (S3), not when say started to wait.
                spoken_by_captain = await asyncio.to_thread(
                    self._count_spoken, since, delivered.typed_at
                )
                reply = delivered.text
                if reply is None:
                    # 13175: a turn of tools alone. The page says so in its own words and
                    # nothing is spoken — there is no reply text, and a placeholder read
                    # aloud would be the captain's words to the owner's ear.
                    await self._send("reply", text=None, spoken=False, note=NO_TEXT_NOTE)
                    ended = await self._end_turn()
                    return
                will_speak = (
                    spoken_by_captain == 0 and bool(reply.strip()) and speaker_mod.speaker_on()
                )
                await self._send("reply", text=reply, spoken=will_speak)
                ended = await self._end_turn()
                if spoken_by_captain == 0:
                    await asyncio.to_thread(self._hooks.voice.utter, reply)
            finally:
                # Whatever path left, or a cancellation mid-turn (S2): the cue never
                # fires late, and the page is never left on "thinking".
                cue.cancel()
                if not ended:
                    with _swallow_socket_errors():
                        await self._end_turn()

    async def _end_turn(self) -> bool:
        """The turn is over: the delivering count drops and the thinking signal follows."""
        self._delivering -= 1
        await self._show_thinking()
        return True

    def _read_home_seq(self) -> int:
        try:
            return self._hooks.home_seq()
        except Exception as exc:  # a courtesy read: without it every reply is spoken, said
            log.warning("captain voice: the home board could not be read: %s", exc)
            return -1

    def _count_spoken(self, since: int, typed_at: datetime | None) -> int:
        if since < 0:
            return 0
        try:
            return self._hooks.spoke_since(since, typed_at)
        except Exception as exc:
            log.warning("captain voice: the speak() audit could not be read: %s", exc)
            return 0

    async def _cue_later(self) -> None:
        with _swallow_cancel():
            await asyncio.sleep(self._hooks.cue_after_s)
            await asyncio.to_thread(self._hooks.voice.utter, CUE_TEXT)

    def _thinking_now(self) -> bool:
        if self._delivering:
            return True
        try:
            return bool(self._hooks.thinking())
        except Exception as exc:  # the signal is a courtesy; the page must not go dark over it
            log.warning("captain voice: the thinking signal could not be read: %s", exc)
            return False

    async def _show_thinking(self) -> None:
        now = await asyncio.to_thread(self._thinking_now)  # tmux and the store, off the loop
        if now != self._shown_thinking:
            self._shown_thinking = now
            await self._send("thinking", on=now)
            if self._hooks.on_thinking is not None:
                self._hooks.on_thinking(now)

    async def _thinking_loop(self) -> None:
        while True:
            await asyncio.sleep(self._hooks.poll_s)
            await self._show_thinking()
            await self._follow_mode_key()


@contextmanager
def _swallow_socket_errors() -> Iterator[None]:
    """A send to a client that is gone is not an event worth a traceback."""
    try:
        yield
    except Exception as exc:
        log.debug("captain voice: the client is gone: %s", exc)


@contextmanager
def _swallow_cancel() -> Iterator[None]:
    with contextlib.suppress(asyncio.CancelledError):
        yield


def build_app(*, token: str, hooks: Hooks | None = None, mode: Mode = "focus") -> Starlette:
    """The ASGI app: the page at ``/``, the websocket at ``/ws``; ``hooks`` on ``app.state``."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import Route, WebSocketRoute

    async def index(request: Request) -> Response:
        return Response(
            page_bytes(),
            media_type="text/html; charset=utf-8",
            headers={"cache-control": "no-cache"},
        )

    async def socket(websocket: WebSocket) -> None:
        await _Connection(
            websocket, token=token, hooks=app.state.hooks, mode=app.state.mode
        ).serve()

    app = Starlette(routes=[Route("/", index), WebSocketRoute("/ws", socket)])
    app.state.hooks = hooks if hooks is not None else Hooks()
    app.state.mode = mode
    return app


def serve(
    *,
    token: str,
    port: int = DEFAULT_PORT,
    host: str = "127.0.0.1",
    mode: Mode = "focus",
    hooks: Hooks | None = None,
) -> None:
    """Run the voice page until interrupted (the CLI's ``captain voice``)."""
    import uvicorn

    app = build_app(token=token, hooks=hooks, mode=mode)
    uvicorn.Server(uvicorn_config(app, host, port)).run()


def uvicorn_config(app: Starlette, host: str, port: int, **overrides: Any) -> Any:
    """The server's own uvicorn settings — one place, so a test can shorten the websocket
    keepalive (``ws_ping_interval``, ``ws_ping_timeout``) and drive the real server."""
    import uvicorn

    settings: dict[str, Any] = {"host": host, "port": port, "log_level": "warning"}
    settings.update(overrides)
    return uvicorn.Config(app, **settings)
