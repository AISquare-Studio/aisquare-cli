"""The voice path against the REAL faster-whisper, on a checked-in fixture.

docs/plans/clixr.md §10. Everything else about this module is tested in
``tests/test_xr_speech.py`` with a fake ``decode``, which is what lets the suite
stay green under the plain ``dev`` extra. This file is the one place the actual
model runs, and it is skipped entirely when the ``xr`` extra is not installed —
which is every contributor machine and every CI job today.

What it is really measuring is the SEAM: PCM16LE bytes in the client's own 20 ms
frames, through the gate and the buffer, into whisper, and out as text. A green
`test_xr_speech.py` proves the buffer is correct against a fake; it cannot prove
that the bytes this module hands whisper are the bytes whisper expects — a
wrong dtype, a missed /32768.0 or a byte-order slip all produce a transcriber
that returns confident nonsense, and only a real decode of a known phrase
catches that.

The fixture is generated, not recorded (see the sibling .txt): reproducible, no
real person's voice in the repository, 95 KB.
"""

from __future__ import annotations

import time
import wave
from pathlib import Path

import pytest

from aisquare.services.xr import speech
from aisquare.services.xr.speech import BufferedTranscriber

FIXTURE = Path(__file__).parent / "fixtures" / "xr" / "hello_board.wav"

#: Matched case-insensitively, and deliberately not the whole sentence. A speech
#: model is allowed to differ on punctuation and casing across versions, and a
#: test that pinned the exact string would fail on an upgrade that got no word
#: wrong. These four words are the ones a wrong dtype or a byte-order slip
#: destroys.
KEY_WORDS = ("show", "board", "open", "planner")

#: The budget from the task contract, for the WHOLE utterance: gate, buffer,
#: every interim decode and the final one, over 2.96 s of audio. Not the model
#: load, which happens once per process at ``transcriber()`` and is the
#: expensive part by a wide margin — that is why a server holds one transcriber
#: per client rather than building one per utterance.
BUDGET_SECONDS = 3.0


@pytest.fixture(scope="module")
def utterance() -> bytes:
    """The fixture as raw PCM16LE, with its wire format asserted rather than assumed."""
    with wave.open(str(FIXTURE), "rb") as source:
        assert source.getframerate() == speech.SAMPLE_RATE, "the fixture is not 16 kHz"
        assert source.getnchannels() == 1, "the fixture is not mono"
        assert source.getsampwidth() == speech.SAMPLE_BYTES, "the fixture is not PCM16"
        return source.readframes(source.getnframes())


@pytest.fixture(scope="module")
def live() -> speech.Transcriber:
    """One real transcriber for this module: the model load is paid once.

    THE SKIP LIVES HERE, not at module level, and the difference is not
    cosmetic. A module-level ``importorskip`` aborts the import, so pytest never
    collects these tests at all — and ``tests/test_every_test_can_fail.py``
    reads this directory with ``ast`` and compares it against what pytest
    collects, so four tests would sit in its audit as functions that are
    checked and never run. It caught exactly that, which is the guard working.
    Skipping from the fixture keeps the node ids real: they are reported as
    skipped, which is the honest answer, and the sweep can see them.
    """
    pytest.importorskip("faster_whisper", reason="the xr extra is not installed")
    return speech.transcriber()


def frames(pcm: bytes) -> list[bytes]:
    """The utterance in the client's own 20 ms frames (§10), the last one short."""
    return [pcm[at : at + speech.FRAME_BYTES] for at in range(0, len(pcm), speech.FRAME_BYTES)]


def test_the_fixture_is_small_enough_to_live_in_the_repository() -> None:
    assert FIXTURE.stat().st_size <= 200_000, "the fixture outgrew its budget"
    assert FIXTURE.with_suffix(".txt").exists(), (
        "the fixture must state its phrase in a sibling .txt, or a failure here "
        "cannot be read without listening to a file"
    )


def test_the_real_backend_yields_an_interim_and_then_the_phrase(
    live: speech.Transcriber, utterance: bytes, capsys: pytest.CaptureFixture[str]
) -> None:
    """The end-to-end claim: 20 ms frames in, interim feedback, final transcript out.

    The interim is not a nice-to-have being checked for completeness. Without
    on-panel text while they speak, an operator cannot tell whether the mic is
    live and will repeat themselves (§10) — so "at least one interim arrived" is
    a functional requirement of the demo, and it is asserted BEFORE the final
    text, in the order the operator experiences them.
    """
    chunks = frames(utterance)
    assert len(chunks) > 100, f"the fixture is too short to earn an interim: {len(chunks)} frames"

    started = time.perf_counter()
    interims = [text for chunk in chunks if (text := live.feed(chunk)) is not None]
    final = live.finish()
    elapsed = time.perf_counter() - started

    with capsys.disabled():
        print(
            f"\n  xr speech: {len(utterance) / (speech.SAMPLE_RATE * speech.SAMPLE_BYTES):.2f}s "
            f"of audio in {len(chunks)} frames of {speech.FRAME_MS}ms -> "
            f"{len(interims)} interim(s), final {final!r} in {elapsed:.2f}s "
            f"(model {speech.model_name()}, cpu/int8; model load excluded)"
        )

    assert interims, "no interim text arrived — the operator gets no sign the mic is live"
    assert all(text.strip() for text in interims), f"an empty interim was emitted: {interims}"

    spoken = final.lower()
    missing = [word for word in KEY_WORDS if word not in spoken]
    assert not missing, f"the final transcript {final!r} is missing {missing}"

    assert elapsed < BUDGET_SECONDS, (
        f"the utterance took {elapsed:.2f}s, over the {BUDGET_SECONDS}s budget — "
        "voice is command input, where latency dominates accuracy (§10)"
    )


def test_a_real_decode_of_silence_produces_no_prompt(live: speech.Transcriber) -> None:
    """The gate, end to end: room tone must never become a prompt.

    Cheap here because it never reaches the model at all — which is the point
    being made. A transcriber that hallucinated a sentence out of silence would
    send it to a live agent session.
    """
    quiet = bytes(speech.FRAME_BYTES)

    interims = [live.feed(quiet) for _ in range(150)]  # three seconds

    assert interims == [None] * 150
    assert live.finish() == ""


def test_the_factory_returns_a_buffered_transcriber_over_the_real_model(
    live: speech.Transcriber,
) -> None:
    """The factory is not quietly handing back a fake when the backend is present."""
    assert isinstance(live, BufferedTranscriber)
    assert isinstance(live, speech.Transcriber)
