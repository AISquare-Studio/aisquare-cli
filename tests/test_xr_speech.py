"""The voice intake: the silence gate, the interim cadence, and the lazy backend.

docs/plans/clixr.md §10. Everything here runs under the plain ``dev`` extra,
with no faster-whisper installed, because :class:`BufferedTranscriber` takes a
``decode`` callable and knows nothing about whisper. The tests that need the
real backend live in ``tests/test_xr_speech_backend.py`` behind an
``importorskip``.

The load-bearing test in this file is
``test_importing_the_speech_module_does_not_import_faster_whisper``. Everything
else here is about behaviour; that one is about whether this package can still
be imported on the Claude Code hook path at all.
"""

from __future__ import annotations

import subprocess
import sys
from array import array
from pathlib import Path

import pytest

from aisquare.services.xr import speech
from aisquare.services.xr.speech import (
    BufferedTranscriber,
    FakeTranscriber,
    SpeechUnavailable,
    Transcriber,
)

# --- audio helpers: PCM16LE by construction, not by fixture ---------------------------


def _silence(ms: int) -> bytes:
    return bytes(speech.SAMPLE_RATE * speech.SAMPLE_BYTES * ms // 1000)


def _tone(ms: int, amplitude: int = 8000) -> bytes:
    """A square wave loud enough to be speech — the gate reads level, not shape."""
    samples = speech.SAMPLE_RATE * ms // 1000
    out = bytearray()
    for index in range(samples):
        value = amplitude if (index // 40) % 2 == 0 else -amplitude
        out += int(value).to_bytes(2, "little", signed=True)
    return bytes(out)


class Decoder:
    """A ``decode`` that returns scripted text and records what it was handed."""

    def __init__(self, *texts: str) -> None:
        self._texts = list(texts)
        self.calls: list[bytes] = []

    def __call__(self, pcm: bytes) -> str:
        self.calls.append(pcm)
        if not self._texts:
            return "hello board"
        return self._texts.pop(0) if len(self._texts) > 1 else self._texts[0]


# --- rms ------------------------------------------------------------------------------


def test_rms_separates_room_tone_from_speech() -> None:
    assert speech.rms(_silence(20)) == 0.0
    assert speech.rms(_tone(20)) > speech.SILENCE_RMS


def test_rms_survives_a_frame_split_mid_sample() -> None:
    """A websocket frame can end on an odd byte; the audio path must not die for it."""
    odd = _tone(20) + b"\x01"

    assert speech.rms(odd) > speech.SILENCE_RMS
    assert speech.rms(b"") == 0.0
    assert speech.rms(b"\x01") == 0.0


# --- the gate: silence never reaches the model -----------------------------------------


def test_silence_before_speech_never_reaches_the_decoder() -> None:
    """The whole point of the gate: a held trigger in a quiet room costs nothing."""
    decode = Decoder("hello board")
    transcriber = BufferedTranscriber(decode)

    for _ in range(100):  # two seconds of room tone
        assert transcriber.feed(_silence(20)) is None

    assert decode.calls == [], "silence was sent to the model"
    assert transcriber.finish() == ""
    assert decode.calls == [], "a silent utterance still cost a decode"


def test_silence_after_speech_is_kept() -> None:
    """The gate is one-way: cutting the pauses out of a sentence is worse input.

    Measured on the buffer the decoder is handed, not on an internal flag —
    what matters is the audio whisper sees.
    """
    decode = Decoder("hello board")
    transcriber = BufferedTranscriber(decode)

    transcriber.feed(_tone(20))
    for _ in range(10):
        transcriber.feed(_silence(20))
    transcriber.finish()

    assert len(decode.calls) == 1
    assert len(decode.calls[0]) == speech.FRAME_BYTES * 11, (
        "the trailing silence was dropped — a gate that fires per frame clips "
        "the quiet consonant that ends a word"
    )


# --- the interim cadence ----------------------------------------------------------------


def test_an_interim_arrives_after_about_a_second_of_speech() -> None:
    decode = Decoder("hello")
    transcriber = BufferedTranscriber(decode)
    frames = speech.INTERIM_BYTES // speech.FRAME_BYTES

    early = [transcriber.feed(_tone(speech.FRAME_MS)) for _ in range(frames - 1)]
    due = transcriber.feed(_tone(speech.FRAME_MS))

    assert early == [None] * (frames - 1), "an interim fired before a second of speech"
    assert due == "hello"
    assert len(decode.calls) == 1


def test_an_unchanged_interim_is_not_re_sent() -> None:
    """Whisper re-decoding a longer buffer returns the same string; the panel must not repaint."""
    decode = Decoder("hello board")
    transcriber = BufferedTranscriber(decode)
    frames = speech.INTERIM_BYTES // speech.FRAME_BYTES

    first = [transcriber.feed(_tone(speech.FRAME_MS)) for _ in range(frames)][-1]
    second = [transcriber.feed(_tone(speech.FRAME_MS)) for _ in range(frames)][-1]

    assert first == "hello board"
    assert second is None, "the same text was sent twice"
    assert len(decode.calls) == 2, "the second decode did not happen at all"


def test_a_changed_interim_is_sent() -> None:
    """The negative control for the test above: new text must still get through."""
    decode = Decoder("hello", "hello board")
    transcriber = BufferedTranscriber(decode)
    frames = speech.INTERIM_BYTES // speech.FRAME_BYTES

    first = [transcriber.feed(_tone(speech.FRAME_MS)) for _ in range(frames)][-1]
    second = [transcriber.feed(_tone(speech.FRAME_MS)) for _ in range(frames)][-1]

    assert (first, second) == ("hello", "hello board")


def test_finish_returns_the_final_transcript_and_resets() -> None:
    """One transcriber serves successive utterances — the model load is the expensive part."""
    decode = Decoder("hello board")
    transcriber = BufferedTranscriber(decode)

    transcriber.feed(_tone(40))
    first = transcriber.finish()
    second_interim = transcriber.feed(_silence(40))
    second = transcriber.finish()

    assert first == "hello board"
    assert second_interim is None, "the reset did not close the gate"
    assert second == "", "the second utterance inherited the first one's buffer"


def test_the_decoder_is_handed_the_whole_utterance() -> None:
    """Whisper has no streaming API: a decode is over the buffer, not a delta.

    Below :data:`speech.INTERIM_WINDOW_BYTES`, which is every utterance this
    path is for, "the buffer so far" and "the window" are the same bytes. The
    test that pins them apart is
    ``test_an_interim_decodes_a_bounded_window_however_long_the_press_runs``.
    """
    decode = Decoder("hello board")
    transcriber = BufferedTranscriber(decode, interim_bytes=speech.FRAME_BYTES * 2)

    for _ in range(4):
        transcriber.feed(_tone(speech.FRAME_MS))

    assert [len(call) for call in decode.calls] == [
        speech.FRAME_BYTES * 2,
        speech.FRAME_BYTES * 4,
    ]


# --- a frame that splits mid-sample ----------------------------------------------------


def _int16_decode(pcm: bytes) -> str:
    """Production's decode shape: read the buffer as int16 and refuse a half sample.

    ``speech.py``'s real decoder opens with
    ``numpy.frombuffer(pcm, dtype=numpy.int16)``, which raises on a length that
    is not a multiple of two. numpy is not a test dependency — the ``xr`` extra
    is optional and this file runs without it — so the constraint is restated
    rather than imported. ``array`` raises for exactly the same reason on
    exactly the same lengths.
    """
    array("h").frombytes(pcm)
    return "hello board"


def test_a_frame_that_splits_mid_sample_loses_and_duplicates_nothing() -> None:
    """The odd byte is carried into the next chunk, not dropped and not concatenated raw.

    Dropping it would be the quiet wrong answer: every sample after the split
    would be assembled from the wrong pair of bytes, which is noise that
    decodes as nothing rather than an error anyone can see. So the assertion is
    on the BYTES, not on the absence of a raise — what the decoder receives has
    to be the audio that was sent, in order, entire.
    """
    speech_bytes = _tone(200)
    decode = Decoder("hello board")
    transcriber = BufferedTranscriber(decode, interim_bytes=speech.FRAME_BYTES)

    cut = 0
    for size in (639, 1, 640, 321, 319, 641):  # odd splits, in and out of alignment
        transcriber.feed(speech_bytes[cut : cut + size])
        cut += size
    transcriber.feed(speech_bytes[cut:])

    assert transcriber.finish() == "hello board"
    assert decode.calls, "an interim was due long before the end of a 200 ms tone"
    assert all(len(call) % speech.SAMPLE_BYTES == 0 for call in decode.calls), (
        "every decode lands on a sample boundary"
    )
    assert decode.calls[-1] == speech_bytes, "the decoder saw the audio, whole and in order"


def test_one_odd_frame_does_not_poison_every_decode_after_it() -> None:
    """The parity of a raw-concatenated buffer never recovers on its own.

    One 639-byte frame among 640s is enough: the buffer is odd from then on and
    the next ``frombuffer`` raises, and so does the one after that, and so does
    ``finish``. That is the whole sentence lost rather than one sample, which
    is why the carry is in ``feed`` and not a ``try`` around the decode.
    """
    transcriber = BufferedTranscriber(_int16_decode)

    transcriber.feed(_tone(20) + b"\x01")
    for _ in range(speech.INTERIM_BYTES // speech.FRAME_BYTES + 1):
        transcriber.feed(_tone(speech.FRAME_MS))

    assert transcriber.finish() == "hello board"


def test_a_lone_odd_byte_waits_for_the_partner_that_completes_it() -> None:
    """A one-byte frame is not audio yet, and must not be treated as any."""
    decode = Decoder("hello board")
    transcriber = BufferedTranscriber(decode, interim_bytes=speech.SAMPLE_BYTES)
    loud = _tone(20)

    assert transcriber.feed(loud[:1]) is None, "half a sample cannot open the gate"
    assert not decode.calls
    transcriber.feed(loud[1:])

    assert decode.calls[-1] == loud, "the held byte led the chunk it belongs to"


def test_a_held_byte_does_not_cross_into_the_next_utterance() -> None:
    """``finish`` resets the carry with everything else, or one press bleeds into the next."""
    decode = Decoder("hello board")
    transcriber = BufferedTranscriber(decode, interim_bytes=speech.SAMPLE_BYTES)

    transcriber.feed(_tone(20) + b"\x01")
    assert transcriber.finish() == "hello board"
    transcriber.feed(_tone(20))

    assert decode.calls[-1] == _tone(20), "the second utterance starts on a sample boundary"


# --- the interim window ----------------------------------------------------------------


def test_an_interim_decodes_a_bounded_window_however_long_the_press_runs() -> None:
    """Re-decoding the whole buffer every second makes one press cost O(n^2).

    The cap is 60 seconds (``server.MAX_UTTERANCE_S``), and unbounded that is
    1890 audio-seconds handed to the decoder for one legal press — minutes of
    CPU, on a path whose read loop is sequential, so the ``audioEnd`` that
    would end it queues behind the backlog. Bounded, no single interim can
    exceed the window no matter how long the operator talks.
    """
    decode = Decoder("hello board")
    window = speech.FRAME_BYTES * 4
    transcriber = BufferedTranscriber(decode, interim_bytes=speech.FRAME_BYTES, window_bytes=window)

    for _ in range(20):
        transcriber.feed(_tone(speech.FRAME_MS))

    assert len(decode.calls) == 20, "one interim per frame, at this cadence"
    assert max(len(call) for call in decode.calls) == window, "no interim exceeds the window"
    assert sum(len(call) for call in decode.calls) < speech.FRAME_BYTES * 20 * 20 // 2, (
        "the total is linear in the utterance, not quadratic"
    )


def test_finish_still_decodes_the_whole_utterance() -> None:
    """The window is an interim economy. The transcript that becomes a prompt is not windowed.

    This is the line between "the panel shows a rolling tail" — acceptable, it
    is a liveness signal — and "the operator's sentence is truncated before it
    is routed", which would not be.
    """
    decode = Decoder("hello board")
    transcriber = BufferedTranscriber(decode, window_bytes=speech.FRAME_BYTES)

    for _ in range(10):
        transcriber.feed(_tone(speech.FRAME_MS))
    transcriber.finish()

    assert len(decode.calls[-1]) == speech.FRAME_BYTES * 10, "finish saw all ten frames"


# --- FakeTranscriber ---------------------------------------------------------------------


def test_the_fake_returns_its_canned_text_and_records_what_it_was_fed() -> None:
    fake = FakeTranscriber("open the planner", interim_bytes=speech.FRAME_BYTES)

    interim = fake.feed(_tone(speech.FRAME_MS))
    again = fake.feed(_tone(speech.FRAME_MS))

    assert interim == "open the planner"
    assert again is None, "the fake emitted the same interim twice"
    assert fake.finish() == "open the planner"
    assert fake.finished is True
    assert bytes(fake.fed) == _tone(speech.FRAME_MS) * 2, (
        "the fake did not record the audio — 'the transcript came back' and 'the "
        "frames arrived' are different claims"
    )


def test_a_silent_fake_returns_empty_text() -> None:
    fake = FakeTranscriber()

    assert fake.feed(_tone(100)) is None
    assert fake.finish() == ""


def test_both_implementations_satisfy_the_protocol() -> None:
    """Checked statically by the annotations and at runtime by the protocol."""
    implementations: list[Transcriber] = [BufferedTranscriber(Decoder()), FakeTranscriber("x")]

    for implementation in implementations:
        assert isinstance(implementation, Transcriber)


# --- the factory ---------------------------------------------------------------------------


def test_the_model_name_comes_from_the_argument_then_the_env_then_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(speech.ENV_MODEL, raising=False)
    assert speech.model_name() == speech.DEFAULT_MODEL
    assert speech.model_name("small.en") == "small.en"

    monkeypatch.setenv(speech.ENV_MODEL, "small.en")
    assert speech.model_name() == "small.en"
    assert speech.model_name("base.en") == "base.en", "the argument must win over the env"


def test_an_unsupported_model_is_refused_with_the_supported_ones_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A big model here would trade the latency this path exists to protect (§10)."""
    monkeypatch.setenv(speech.ENV_MODEL, "large-v3")

    with pytest.raises(SpeechUnavailable) as raised:
        speech.transcriber()

    assert "large-v3" in raised.value.reason
    assert "base.en" in raised.value.fix and "small.en" in raised.value.fix


def test_a_missing_backend_is_a_speech_unavailable_carrying_the_install_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cost of the lazy import, made survivable.

    ``sys.modules[name] = None`` is how an import is blocked for a module that
    may or may not be installed on the machine running the suite — so this test
    measures the same thing with the extra present and absent.
    """
    monkeypatch.delenv(speech.ENV_MODEL, raising=False)
    monkeypatch.setitem(sys.modules, "faster_whisper", None)

    with pytest.raises(SpeechUnavailable) as raised:
        speech.transcriber()

    assert "faster-whisper is not installed" in raised.value.reason
    assert "[xr]" in raised.value.fix, "the error names the problem but not the fix"


def test_the_download_fix_is_a_pasteable_one_liner() -> None:
    fix = speech.download_fix("small.en")

    assert fix.count("\n") == 0
    assert "small.en" in fix and "python -c" in fix


# --- the laziness itself --------------------------------------------------------------------


@pytest.mark.parametrize(
    "module",
    ["aisquare.services.xr.speech", "aisquare.services.diagnostics", "aisquare.cli.app"],
)
def test_importing_the_speech_module_does_not_import_faster_whisper(module: str) -> None:
    """THE PROPERTY THIS MODULE'S SHAPE EXISTS FOR, measured rather than intended.

    faster-whisper drags in ctranslate2, av and numpy. `aisquare` is on the
    Claude Code hook path, which runs in front of a developer who has just hit
    enter, and it must also import in an environment where the xr extra is not
    installed at all — which is every dev machine and every CI job.

    A SUBPROCESS, not an in-process assertion: by the time this file runs,
    another test may already have put faster_whisper in ``sys.modules``, and
    then an in-process check would pass no matter where the import lives.
    ``diagnostics`` and the CLI app are here too because the doctor check
    imports this module, so a module-level import would travel from here into
    every single command.
    """
    probe = f"import sys; import {module}; sys.exit(1 if 'faster_whisper' in sys.modules else 0)"

    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
    )

    assert result.returncode == 0, (
        f"importing {module} pulled faster_whisper into sys.modules "
        f"(exit {result.returncode}): {result.stderr}"
    )
