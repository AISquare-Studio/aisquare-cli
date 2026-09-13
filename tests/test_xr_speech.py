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
    """Whisper has no streaming API: every decode is over the buffer so far."""
    decode = Decoder("hello board")
    transcriber = BufferedTranscriber(decode, interim_bytes=speech.FRAME_BYTES * 2)

    for _ in range(4):
        transcriber.feed(_tone(speech.FRAME_MS))

    assert [len(call) for call in decode.calls] == [
        speech.FRAME_BYTES * 2,
        speech.FRAME_BYTES * 4,
    ]


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
    probe = (
        f"import sys; import {module}; "
        "sys.exit(1 if 'faster_whisper' in sys.modules else 0)"
    )

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
