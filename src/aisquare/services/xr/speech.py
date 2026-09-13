"""Voice intake for cliXR: 16 kHz mono PCM16 in, text out.

The client holds the left trigger, an ``AudioWorklet`` ships 20 ms frames of
16 kHz mono PCM16LE over the websocket as binary, and this module turns them
into an interim string for the focus panel and a final transcript for the
prompt (docs/plans/clixr.md §10). Nothing here knows about websockets; the
server task wires it.

Three decisions worth stating, because each one was a choice:

**faster-whisper is imported LAZILY, inside the factory.** Never at module
import. `aisquare` is on the Claude Code hook path — five lifecycle hooks run
this package in front of a developer who has just hit enter — and
faster-whisper drags in ctranslate2, av and numpy, which is hundreds of
milliseconds of import on a path whose whole budget is smaller than that. It is
also not installed at all under the plain ``dev`` extra, so a module-level
import would make this file unimportable in the environment every contributor
and CI job runs. The cost of the laziness is that a missing backend is found at
``transcriber()`` rather than at import; that is what :class:`SpeechUnavailable`
is for, and it carries the fix rather than just the complaint.

**The buffer, the gate and the cadence do not know what a model is.**
:class:`BufferedTranscriber` takes a ``decode`` callable. That is what makes the
silence gate and the interim cadence testable with no backend installed, and it
is why this module has exactly one third-party import in it.

**A SMALL model, decoded greedily.** ``base.en``/``small.en``, CPU, ``int8``,
``beam_size=1``. This path is command input, where latency dominates accuracy
(§10); long-form transcription is a different job for a different model and is
not in this plan.
"""

from __future__ import annotations

import os
import sys
from array import array
from collections.abc import Callable
from math import sqrt
from typing import Protocol, runtime_checkable

from aisquare.core.version import DISTRIBUTION

#: The wire format, fixed by the client's ``AudioWorklet`` (§10). Every byte
#: length in this module is derived from these rather than written out, so a
#: change to the client's frame size has one place to land.
SAMPLE_RATE = 16_000
SAMPLE_BYTES = 2
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * SAMPLE_BYTES * FRAME_MS // 1000

#: Which model the backend loads. ``base.en`` is the default because it is the
#: fastest thing that reliably hears a short English command; ``small.en`` is
#: the one step up an operator may want on a fast machine. Anything else is
#: refused rather than passed through to a download of an unknown size — the
#: allowed set IS the plan's ruling (§10), not a guess about what fits.
ENV_MODEL = "AISQUARE_XR_WHISPER_MODEL"
DEFAULT_MODEL = "base.en"
ALLOWED_MODELS = ("base.en", "small.en")

#: How much buffered SPEECH earns an interim decode. The operator needs to see
#: that the mic is live or they will repeat themselves (§10); one second is
#: fast enough to answer that and slow enough that a ``base.en`` decode on CPU
#: finishes long before the next one is due.
INTERIM_SECONDS = 1.0
INTERIM_BYTES = int(SAMPLE_RATE * SAMPLE_BYTES * INTERIM_SECONDS)

#: RMS below which a frame is treated as room tone. 16-bit speech at a
#: headset mic sits in the thousands; a quiet room sits in the low hundreds.
#: Deliberately a floor rather than an adaptive gate: the cost of getting it
#: slightly wrong is one extra decode of near-silence, and an adaptive gate
#: that mis-calibrates on the first frame costs the operator their first word.
SILENCE_RMS = 350.0

#: How to get the backend, and how to pre-fetch a model. Both are stated as
#: commands because a diagnostic that names a problem without the line that
#: fixes it is the thing this codebase's doctor idiom exists to avoid.
INSTALL_FIX = (
    f"Install the XR extra into the same environment as aisquare: "
    f"pip install '{DISTRIBUTION}[xr]'"
)


def download_fix(model: str = DEFAULT_MODEL) -> str:
    """The one-liner that pre-downloads ``model`` into the Hugging Face cache.

    Shared with the ``xr`` doctor check so the two cannot drift: the first time
    a model is used it is fetched from the network, and a demo is the worst
    moment to discover that. Quoted for a shell, on one line, pasteable.
    """
    return (
        f'Pre-download it once: python -c "from faster_whisper import WhisperModel; '
        f"WhisperModel('{model}', device='cpu', compute_type='int8')\""
    )


def model_name(explicit: str | None = None) -> str:
    """Which model this machine is configured to use: argument, env, default.

    Resolution only — an unknown name is returned as configured rather than
    silently replaced, so both :func:`transcriber` and the doctor check can
    say *which* wrong name was set instead of reporting the default and
    leaving the operator's export invisible.
    """
    if explicit and explicit.strip():
        return explicit.strip()
    return os.environ.get(ENV_MODEL, "").strip() or DEFAULT_MODEL


class SpeechUnavailable(RuntimeError):
    """Voice cannot start, with the fix attached.

    Carries ``reason`` and ``fix`` as separate attributes because the server
    task has to put them in a ``{"t": "error"}`` frame, where a pre-joined
    sentence would have to be split apart again. ``str()`` joins them for a log
    line and for anything that just prints the exception.
    """

    def __init__(self, reason: str, fix: str) -> None:
        super().__init__(f"{reason} — {fix}")
        self.reason = reason
        self.fix = fix


@runtime_checkable
class Transcriber(Protocol):
    """Audio in, text out — the whole contract the server codes against."""

    def feed(self, pcm: bytes) -> str | None:
        """Take one chunk of 16 kHz mono PCM16LE.

        Returns interim text when this chunk produced something NEW to show on
        the focus panel, and ``None`` otherwise — which is the common case, so
        a caller can forward the result straight into an ``stt`` frame without
        deciding anything itself.
        """

    def finish(self) -> str:
        """End the utterance and return the final transcript (``""`` if silent)."""


def rms(pcm: bytes) -> float:
    """Root-mean-square level of a PCM16LE chunk; 0.0 for an empty one.

    A trailing odd byte is dropped rather than raising: a websocket frame can
    split anywhere, and a diagnostic-free crash in the audio path would take
    down a voice prompt for one byte of misalignment.

    The byteswap is not decoration. ``array("h")`` reads in NATIVE order and
    the wire format is little-endian, so on a big-endian machine every sample
    would be read with its bytes reversed — which turns quiet room tone into
    levels that sail past any threshold, and the gate would let everything
    through while looking like it worked.
    """
    usable = len(pcm) - (len(pcm) % SAMPLE_BYTES)
    if usable <= 0:
        return 0.0
    samples = array("h")
    samples.frombytes(pcm[:usable])
    if sys.byteorder == "big":
        samples.byteswap()
    return sqrt(sum(sample * sample for sample in samples) / len(samples))


class BufferedTranscriber:
    """The gate, the buffer and the interim cadence over any ``decode``.

    ``decode`` takes the whole utterance so far as PCM16LE bytes and returns
    its transcript. Whisper has no streaming API — every interim is a full
    re-decode of a slightly longer buffer — so this is the shape the backend
    actually has, not an abstraction over a streaming one it does not.

    Reusable across utterances: :meth:`finish` resets the buffer, so a server
    can hold ONE of these per client and pay the model load once. That is the
    expensive part by a wide margin.
    """

    def __init__(
        self,
        decode: Callable[[bytes], str],
        *,
        silence_rms: float = SILENCE_RMS,
        interim_bytes: int = INTERIM_BYTES,
    ) -> None:
        self._decode = decode
        self._silence_rms = silence_rms
        self._interim_bytes = interim_bytes
        self._buffer = bytearray()
        self._speaking = False
        self._decoded_at = 0
        self._last = ""

    def feed(self, pcm: bytes) -> str | None:
        """Buffer one chunk; decode and return interim text when one is due.

        THE GATE IS ONE-WAY, and that is the point. Frames are dropped until
        one of them is loud enough to be speech; from then on everything is
        kept, silence included. Gating every frame independently would cut the
        pauses out of the middle of a sentence and hand whisper audio with the
        gaps removed — worse input than the original, and it clips the quiet
        consonant that ends a word. What the gate is actually for is the
        operator holding the trigger for a second before they start talking:
        that never reaches the model, which is the whole saving.
        """
        if not pcm:
            return None
        if not self._speaking:
            if rms(pcm) < self._silence_rms:
                return None
            self._speaking = True
        self._buffer.extend(pcm)
        if len(self._buffer) - self._decoded_at < self._interim_bytes:
            return None
        self._decoded_at = len(self._buffer)
        text = self._decode(bytes(self._buffer)).strip()
        # Nothing new is nothing to send. Whisper re-decoding a buffer that
        # grew by a second of silence returns the same string, and forwarding
        # it would repaint the panel for no reason.
        if not text or text == self._last:
            return None
        self._last = text
        return text

    def finish(self) -> str:
        """Final transcript for the utterance, and reset for the next one.

        An utterance that never got past the gate is ``""`` WITHOUT calling the
        backend at all: a trigger pressed and released in a quiet room must not
        cost a decode, and must never produce a prompt out of room tone.
        """
        if not self._buffer:
            self._reset()
            return ""
        text = self._decode(bytes(self._buffer)).strip()
        self._reset()
        return text

    def _reset(self) -> None:
        self._buffer = bytearray()
        self._speaking = False
        self._decoded_at = 0
        self._last = ""


class FakeTranscriber:
    """A :class:`Transcriber` that returns canned text and records what it was fed.

    Ships here rather than in the test suite because the server task needs it
    too: wiring the voice path end to end must be testable without a 140 MB
    model, a download, or a second of CPU per assertion. ``fed`` is kept so a
    caller can assert that the frames actually arrived — "the transcript came
    back" and "the audio reached the transcriber" are different claims, and a
    fake that only answers the first one lets a dropped frame pass.
    """

    def __init__(self, canned: str = "", *, interim_bytes: int = INTERIM_BYTES) -> None:
        self.canned = canned
        self.fed = bytearray()
        self.finished = False
        self._interim_bytes = interim_bytes
        self._emitted = False

    def feed(self, pcm: bytes) -> str | None:
        """Record the chunk; emit the canned text once, on the same cadence as the real one."""
        self.fed.extend(pcm)
        if self._emitted or not self.canned or len(self.fed) < self._interim_bytes:
            return None
        self._emitted = True
        return self.canned

    def finish(self) -> str:
        self.finished = True
        self._emitted = False
        return self.canned


def _whisper_decode(model: str) -> Callable[[bytes], str]:
    """Load faster-whisper and return a ``bytes -> text`` decoder over it.

    THE ONLY PLACE faster-whisper IS IMPORTED. See the module docstring for why
    that matters; in short, every other consumer of this file — the hook path,
    mypy under the plain dev extra, the doctor check that reports whether the
    backend is installed at all — has to be able to import this module without
    it.
    """
    try:
        import numpy
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise SpeechUnavailable(f"faster-whisper is not installed ({exc})", INSTALL_FIX) from exc
    try:
        whisper = WhisperModel(model, device="cpu", compute_type="int8")
    except Exception as exc:
        # The first load of an uncached model goes to the network, so this is
        # also what an offline machine hits. The fix is the pre-download line
        # either way, which is why it is not narrowed to a download failure.
        raise SpeechUnavailable(
            f"the whisper model {model!r} could not be loaded ({exc})", download_fix(model)
        ) from exc

    def decode(pcm: bytes) -> str:
        samples = numpy.frombuffer(pcm, dtype=numpy.int16).astype(numpy.float32) / 32768.0
        segments, _info = whisper.transcribe(
            samples,
            language="en",
            beam_size=1,
            # Each interim re-decodes the whole buffer, so conditioning on the
            # previous result would let one early mishearing steer every
            # decode after it for the rest of the utterance.
            condition_on_previous_text=False,
            # Ours: the RMS gate above already dropped the lead-in silence, and
            # whisper's own VAD would be a second opinion on audio this module
            # has already decided about.
            vad_filter=False,
        )
        return " ".join(str(segment.text).strip() for segment in segments).strip()

    return decode


def transcriber(model: str | None = None) -> Transcriber:
    """A live :class:`Transcriber` backed by faster-whisper on CPU.

    Raises :class:`SpeechUnavailable` — with the fix — when the extra is not
    installed, when the configured model name is not one this path supports, or
    when the model cannot be loaded. Never returns a degraded object: a
    transcriber that silently produces nothing would reach the operator as a
    mic that does not work, with no way to find out why.
    """
    name = model_name(model)
    if name not in ALLOWED_MODELS:
        raise SpeechUnavailable(
            f"{ENV_MODEL}={name!r} is not a supported model for voice prompts",
            f"Voice input is command input, where latency dominates: set "
            f"{ENV_MODEL} to one of {', '.join(ALLOWED_MODELS)}",
        )
    return BufferedTranscriber(_whisper_decode(name))
