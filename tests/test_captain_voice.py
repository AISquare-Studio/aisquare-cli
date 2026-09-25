"""The captain's voice page (card T3): both modes end to end over the websocket, on a fake
transcriber.

The acceptance lines are the test names: FakeTranscriber end to end in both modes
(frames in, final text out, the delivery recorded), always-listening splitting
two utterances on silence, the thinking signal pinned, and the seams the runner
drives live (the page, the URL, the QR). Every server task runs with short
polls, every wait is bounded, and nothing here opens a microphone, a model or a
tmux pane: ``brain.say`` is a recorder, the Speaker a recorder, the transcriber
a fake — the shape T1's tests and cliXR's took.
"""

from __future__ import annotations

import contextlib
import json
import logging
import socket
import sys
import threading
import time
from array import array
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from aisquare.services.captain import brain, voice
from aisquare.services.captain import speaker as speaker_mod
from aisquare.services.captain import state as captain_state
from aisquare.services.captain.voice import (
    FRAME_BYTES,
    INTERIM_BYTES,
    SILENCE_BYTES,
    DeliveryFailed,
    FakeTranscriber,
    Hooks,
    Segmenter,
    build_app,
    is_stop_word,
)

TOKEN = "tok-3d-test"
RECEIVE_TIMEOUT_S = 10.0
LOUD = array("h", [3000, -3000] * (FRAME_BYTES // 4)).tobytes()
"""One 20 ms frame well over the RMS gate."""
QUIET = bytes(FRAME_BYTES)
"""One 20 ms frame of room tone."""
FRAMES_PER_SECOND = INTERIM_BYTES // FRAME_BYTES
FRAMES_OF_SILENCE = SILENCE_BYTES // FRAME_BYTES + 1

assert FRAME_BYTES == 640 and len(LOUD) == 640


class Spoken:
    """A Speaker that records instead of playing."""

    name = "recorder"

    def __init__(self) -> None:
        self.lines: list[str] = []

    def utter(self, text: str) -> None:
        self.lines.append(text)


class Deliveries:
    """``brain.say`` as a recorder: what was delivered, answered as told."""

    def __init__(
        self,
        reply: str | None = "done",
        *,
        delay_s: float = 0.0,
        fail: str | None = None,
        raises: Exception | None = None,
        during: Callable[[], None] | None = None,
        before_typing: Callable[[], None] | None = None,
    ) -> None:
        self.texts: list[str] = []
        self.reply = reply
        self.delay_s = delay_s
        self.fail = fail
        self.raises = raises  # any other exception out of the delivery seam (S2)
        self.during = during  # what the captain does inside the turn (a speak() call)
        self.before_typing = before_typing  # what happens while say waits to type (S3)
        self.typed_at: list[datetime] = []

    def __call__(self, text: str) -> voice.Delivered:
        self.texts.append(text)
        if self.before_typing is not None:
            self.before_typing()
        typed = datetime.now(tz=UTC)
        self.typed_at.append(typed)
        if self.during is not None:
            self.during()
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.fail:
            raise DeliveryFailed(self.fail)
        if self.raises is not None:
            raise self.raises
        return voice.Delivered(self.reply, typed_at=typed)


class Harness:
    def __init__(
        self,
        *,
        canned: str | list[str] = "approve the deploy",
        deliveries: Deliveries | None = None,
        thinking: Callable[[], bool] | None = None,
        cue_after_s: float = 60.0,
        mode: voice.Mode = "focus",
    ) -> None:
        self.transcribers: list[FakeTranscriber] = []
        self.spoken = Spoken()
        self.deliveries = deliveries or Deliveries()
        self.thinking_flag = False
        self.thinking_flips: list[bool] = []  # the CLI's side of the signal (Hooks.on_thinking)
        self.seq = 100  # the home board's latest seq; captain_speaks() moves it
        self.speak_seqs: list[tuple[int, datetime]] = []  # the captain's speak() audits
        self.board_broken: str | None = None
        self.mode_key: voice.Mode | None = None  # state.json captain_voice_mode, in memory
        self.mode_writes: list[voice.Mode] = []

        def factory() -> FakeTranscriber:
            fake = FakeTranscriber(canned)
            self.transcribers.append(fake)
            return fake

        def home_seq() -> int:
            if self.board_broken is not None:
                raise OSError(self.board_broken)
            return self.seq

        def spoke_since(since: int, typed_at: datetime | None) -> int:
            return sum(
                1
                for seq, at in self.speak_seqs
                if seq > since and (typed_at is None or at >= typed_at)
            )

        def set_mode_key(mode: voice.Mode) -> None:
            self.mode_key = mode
            self.mode_writes.append(mode)

        self.hooks = Hooks(
            transcriber_factory=factory,
            deliver=self.deliveries,
            voice=speaker_mod.Voice(self.spoken, enabled=lambda: True),
            thinking=thinking or (lambda: self.thinking_flag),
            voice_mode=lambda: self.mode_key,
            set_voice_mode=set_mode_key,
            home_seq=home_seq,
            spoke_since=spoke_since,
            on_thinking=self.thinking_flips.append,
            cue_after_s=cue_after_s,
            poll_s=0.02,
        )
        self.app = build_app(token=TOKEN, hooks=self.hooks, mode=mode)

    def captain_speaks(self, at: datetime | None = None) -> None:
        """What T1's ``speak()`` leaves behind: one ok ``captain_action`` on the home board."""
        self.seq += 1
        self.speak_seqs.append((self.seq, at or datetime.now(tz=UTC)))


@pytest.fixture
def harness() -> Harness:
    return Harness()


def _text(connection: Any) -> str:
    """The next text frame, bounded: a server that stops sending fails the test, not the run."""
    received: list[str] = []

    def read() -> None:
        received.append(connection.receive_text())

    worker = threading.Thread(target=read, daemon=True)
    worker.start()
    worker.join(RECEIVE_TIMEOUT_S)
    if not received:
        raise AssertionError(
            f"no frame within {RECEIVE_TIMEOUT_S:.0f}s — the server stopped sending"
        )
    return received[0]


def _until(connection: Any, kind: str) -> dict[str, Any]:
    """Frames until one of ``kind``; the others (interims, thinking, utterance) interleave freely.

    Tests that pin an ORDER read the frames themselves; this reads past the chatter."""
    for _ in range(50):
        frame = json.loads(_text(connection))
        if frame["t"] == kind:
            return dict(frame)
    raise AssertionError(f"no {kind!r} frame in 50 frames")


def _authed(client: TestClient) -> Iterator[Any]:
    with client.websocket_connect("/ws") as connection:
        connection.send_text(json.dumps({"t": "auth", "token": TOKEN}))
        hello = json.loads(_text(connection))
        assert hello["t"] == "hello", hello
        yield connection


def _burst(connection: Any, frames: int = FRAMES_PER_SECOND) -> None:
    connection.send_text(json.dumps({"t": "audio", "seq": 1}))
    for _ in range(frames):
        connection.send_bytes(LOUD)
    connection.send_text(json.dumps({"t": "audioEnd"}))


# --- the segmenter --------------------------------------------------------------------------------


def test_the_segmenter_drops_room_tone_and_ends_an_utterance_on_trailing_silence() -> None:
    fake = FakeTranscriber(["first", "second"])
    segmenter = Segmenter(fake)
    for _ in range(5):
        assert segmenter.feed(QUIET) == (None, None)
    assert bytes(fake.fed) == b"" and not segmenter.speaking
    finals: list[str] = []
    for _ in range(FRAMES_PER_SECOND):
        _, final = segmenter.feed(LOUD)
        assert final is None
    assert segmenter.speaking
    for _ in range(FRAMES_OF_SILENCE):
        _, final = segmenter.feed(QUIET)
        if final is not None:
            finals.append(final)
    assert finals == ["first"] and not segmenter.speaking
    for _ in range(FRAMES_PER_SECOND):
        segmenter.feed(LOUD)
    for _ in range(FRAMES_OF_SILENCE):
        _, final = segmenter.feed(QUIET)
        if final is not None:
            finals.append(final)
    assert finals == ["first", "second"] and fake.finished == 2


def test_the_segmenter_flushes_an_open_utterance_and_caps_a_long_one() -> None:
    fake = FakeTranscriber("cut short")
    segmenter = Segmenter(fake, max_bytes=FRAME_BYTES * 3)
    assert segmenter.flush() is None
    segmenter.feed(LOUD)
    assert segmenter.flush() == "cut short" and not segmenter.speaking
    finals = [segmenter.feed(LOUD)[1] for _ in range(3)]
    assert finals == [None, None, "cut short"], "the cap ends the utterance as it stands"


@pytest.mark.parametrize(
    ("text", "stop"),
    [
        ("stop listening", True),
        ("Stop listening.", True),
        ("  STOP   LISTENING!", True),
        ("stop", False),
        ("please stop listening now", False),
    ],
)
def test_the_stop_word_is_the_whole_final_transcript(text: str, stop: bool) -> None:
    assert is_stop_word(text) is stop


# --- focus mode, end to end -----------------------------------------------------------------------


def test_focus_a_held_burst_is_interim_then_final_then_delivered_and_the_reply_is_spoken(
    harness: Harness,
) -> None:
    with TestClient(harness.app) as client:
        for connection in _authed(client):
            connection.send_text(json.dumps({"t": "audio", "seq": 1}))
            for _ in range(FRAMES_PER_SECOND):
                connection.send_bytes(LOUD)
            interim = _until(connection, "stt")
            connection.send_text(json.dumps({"t": "audioEnd"}))
            final = _until(connection, "stt")
            utterance = _until(connection, "utterance")
            reply = _until(connection, "reply")
    assert interim == {"t": "stt", "text": "approve the deploy", "final": False}
    assert final == {"t": "stt", "text": "approve the deploy", "final": True}
    assert utterance["text"] == "approve the deploy"
    assert reply == {"t": "reply", "text": "done", "spoken": True}
    assert harness.deliveries.texts == ["approve the deploy"], "brain.say got the final, once"
    assert harness.spoken.lines == ["done"], "the reply is spoken, nothing else"
    assert len(harness.transcribers) == 1, "one transcriber per connection"
    assert bytes(harness.transcribers[0].fed) == LOUD * FRAMES_PER_SECOND, "every frame arrived"
    assert harness.transcribers[0].finished == 1


def test_focus_a_silent_press_delivers_nothing_and_a_re_press_commits_the_open_burst() -> None:
    harness = Harness(canned=["", "second press"])
    with TestClient(harness.app) as client:
        for connection in _authed(client):
            _burst(connection, frames=2)
            final = _until(connection, "stt")
            assert final["text"] == "" and final["final"] is True
            connection.send_text(json.dumps({"t": "audio", "seq": 2}))
            connection.send_bytes(LOUD)
            connection.send_text(json.dumps({"t": "audio", "seq": 3}))  # a bounce: burst 2 commits
            assert _until(connection, "stt")["text"] == "second press"
            assert _until(connection, "utterance")["text"] == "second press"
            _until(connection, "reply")
            connection.send_bytes(LOUD)
            connection.send_text(json.dumps({"t": "audioEnd"}))
            assert _until(connection, "stt")["text"] == "second press"
            _until(connection, "reply")
    assert harness.deliveries.texts == ["second press", "second press"]
    assert harness.transcribers[0].finished == 3


def test_the_thinking_signal_shows_while_a_delivery_runs_and_follows_the_captains_flag(
    harness: Harness,
) -> None:
    harness.deliveries.delay_s = 0.15
    with TestClient(harness.app) as client:
        for connection in _authed(client):
            _burst(connection)
            seen: list[dict[str, Any]] = []
            while True:
                frame = json.loads(_text(connection))
                seen.append(frame)
                if frame["t"] == "reply":
                    break
            before_reply = seen[: [f["t"] for f in seen].index("reply")]
            assert any(f["t"] == "thinking" and f["on"] is True for f in before_reply), seen
            off = _until(connection, "thinking")
            assert off["on"] is False
            harness.thinking_flag = True  # T1's `thinking on`, or a working pane
            assert _until(connection, "thinking")["on"] is True
            harness.thinking_flag = False
            assert _until(connection, "thinking")["on"] is False
    assert harness.thinking_flips == [True, False, True, False], (
        "the terminal hook saw every flip the page did, and nothing else"
    )


@pytest.mark.parametrize("captain_spoke", [False, True])
def test_the_reply_is_spoken_only_when_the_captain_made_no_speak_call_that_turn(
    captain_spoke: bool,
) -> None:
    """13143 (4): the brain decides what is worth saying. A turn in which the captain called
    ``speak()`` is already audible through the server's drainer, so the page stays quiet;
    a silent turn's reply is spoken so a captain that forgets still answers aloud."""
    harness = Harness(deliveries=Deliveries("all green"))
    harness.captain_speaks()  # an earlier turn's line: not this turn's, never counted
    if captain_spoke:
        harness.deliveries.during = harness.captain_speaks
    with TestClient(harness.app) as client:
        for connection in _authed(client):
            connection.send_text(json.dumps({"t": "text", "text": "how is the fold"}))
            reply = _until(connection, "reply")
    assert reply == {"t": "reply", "text": "all green", "spoken": not captain_spoke}
    assert harness.spoken.lines == ([] if captain_spoke else ["all green"])


def test_a_turn_without_text_shows_the_pages_own_note_and_speaks_nothing() -> None:
    """13175: ``Reply.text`` is None when the captain answered with tools alone. The page
    says so in its own words and speaks nothing — never a placeholder in the captain's voice."""
    harness = Harness(deliveries=Deliveries(None))
    with TestClient(harness.app) as client:
        for connection in _authed(client):
            connection.send_text(json.dumps({"t": "text", "text": "stop coder-2"}))
            reply = _until(connection, "reply")
    assert reply == {"t": "reply", "text": None, "spoken": False, "note": voice.NO_TEXT_NOTE}
    assert harness.spoken.lines == []
    assert harness.deliveries.texts == ["stop coder-2"]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_a_long_turn_in_listen_mode_keeps_the_socket_open_and_the_reply_arrives() -> None:
    """coderp's B1 (comment 5833331924): the delivery blocked the websocket read loop, uvicorn
    stopped reading the page's pongs, and its keepalive closed the socket 20 to 40 s into any
    longer turn with 1011 — the reply frame went to a dead socket. Real uvicorn on loopback,
    a 1 s ping and a 1 s timeout, a four-second turn, and a client that streams room tone the
    whole time without pinging, as a browser does: the socket stays open, the reply arrives."""
    import uvicorn
    from websockets.sync.client import connect

    harness = Harness(mode="listen", deliveries=Deliveries("all quiet", delay_s=4.0))
    port = _free_port()
    config = voice.uvicorn_config(
        harness.app, host="127.0.0.1", port=port, ws_ping_interval=1.0, ws_ping_timeout=1.0
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started, "uvicorn did not come up"
        with connect(f"ws://127.0.0.1:{port}/ws", ping_interval=None) as ws:
            ws.send(json.dumps({"t": "auth", "token": TOKEN}))
            assert json.loads(ws.recv(timeout=5))["t"] == "hello"
            ws.send(json.dumps({"t": "text", "text": "what is up"}))
            reply: dict[str, Any] | None = None
            seen: list[str] = []
            until = time.monotonic() + 9
            while reply is None and time.monotonic() < until:
                ws.send(QUIET)  # the page streams a frame every 20 ms in listen mode
                try:
                    frame = json.loads(ws.recv(timeout=0.02))
                except TimeoutError:
                    continue
                seen.append(frame["t"])
                if frame["t"] == "reply":
                    reply = frame
    finally:
        server.should_exit = True
        thread.join(5)
    assert reply is not None, f"no reply within nine seconds; frames seen: {seen}"
    assert reply["text"] == "all quiet"
    assert harness.deliveries.texts == ["what is up"]


def test_a_delivery_error_of_any_kind_is_said_and_clears_thinking_and_the_cue(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """coderp's S2: only NoReply was caught; anything else left thinking on for the page's life
    and still said "on it". Now every failure is an error frame, the cue is cancelled, and the
    next turn starts clean."""
    harness = Harness(
        deliveries=Deliveries(raises=RuntimeError("the board's disk is on fire")), cue_after_s=0.2
    )
    with (
        caplog.at_level(logging.WARNING, logger="aisquare.services.captain.voice"),
        TestClient(harness.app) as client,
    ):
        for connection in _authed(client):
            connection.send_text(json.dumps({"t": "text", "text": "hello"}))
            error = _until(connection, "error")
            off = _until(connection, "thinking")
            time.sleep(0.4)  # past the cue: nothing may be spoken
            harness.deliveries.raises = None
            connection.send_text(json.dumps({"t": "text", "text": "again"}))
            reply = _until(connection, "reply")
    assert error["code"] == "internal" and "disk is on fire" in error["message"]
    assert off["on"] is False, "thinking is off again after the failure"
    assert harness.spoken.lines == ["done"], "no cue and no line for the failed turn"
    assert reply["text"] == "done", "the next turn starts clean"
    assert harness.thinking_flips == [True, False, True, False]


def test_deliver_to_captain_says_a_fleet_refusal_as_a_failed_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T2's brain.Unreachable after a reboot is a FleetError: the page must say it, not crash."""
    from aisquare.services import fleet

    def unreachable(text: str, *, timeout: float = 180.0) -> brain.Reply:
        raise fleet.FleetError("the captain's row is live but its tmux server does not answer")

    monkeypatch.setattr(brain, "say", unreachable)
    with pytest.raises(voice.DeliveryFailed, match="tmux server does not answer"):
        voice.deliver_to_captain("what is up")


def test_a_speak_from_the_wait_before_the_text_was_typed_does_not_mute_this_reply() -> None:
    """coderp's S3: the window opened before brain.say waited for the lock or a busy captain,
    so another turn's speak() muted this reply. It opens when the text is typed."""
    harness = Harness(deliveries=Deliveries("all green"))
    harness.deliveries.before_typing = lambda: harness.captain_speaks(
        at=datetime.now(tz=UTC)
    )  # the busy turn's own speak(), landing while say waits to type
    with TestClient(harness.app) as client:
        for connection in _authed(client):
            connection.send_text(json.dumps({"t": "text", "text": "how is the fold"}))
            reply = _until(connection, "reply")
    assert reply["spoken"] is True and harness.spoken.lines == ["all green"]


def test_when_the_home_board_cannot_be_read_the_reply_is_still_spoken_and_it_is_said(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = Harness()
    harness.board_broken = "the board's disk is on fire"
    with (
        caplog.at_level(logging.WARNING, logger="aisquare.services.captain.voice"),
        TestClient(harness.app) as client,
    ):
        for connection in _authed(client):
            connection.send_text(json.dumps({"t": "text", "text": "hello"}))
            reply = _until(connection, "reply")
    assert reply["spoken"] is True and harness.spoken.lines == ["done"], "audible beats silent"
    assert any("disk is on fire" in r.getMessage() for r in caplog.records), "never quietly"


def test_a_slow_reply_earns_one_spoken_cue_before_it_arrives() -> None:
    harness = Harness(deliveries=Deliveries("here it is", delay_s=0.3), cue_after_s=0.05)
    with TestClient(harness.app) as client:
        for connection in _authed(client):
            _burst(connection)
            _until(connection, "reply")
    assert harness.spoken.lines == ["on it", "here it is"]


def test_a_quick_reply_earns_no_cue() -> None:
    harness = Harness(deliveries=Deliveries("quick"), cue_after_s=0.5)
    with TestClient(harness.app) as client:
        for connection in _authed(client):
            _burst(connection)
            _until(connection, "reply")
    assert harness.spoken.lines == ["quick"]


def test_no_reply_is_an_error_frame_and_a_spoken_line_never_a_crash() -> None:
    harness = Harness(deliveries=Deliveries(fail="the captain did not answer within 180s"))
    with TestClient(harness.app) as client:
        for connection in _authed(client):
            _burst(connection)
            error = _until(connection, "error")
            assert error["code"] == "no_reply" and "180s" in error["message"]
            connection.send_text(json.dumps({"t": "text", "text": "still alive?"}))
            _until(connection, "error")  # the socket is still open and answering
    assert harness.spoken.lines == ["the captain did not answer"] * 2


def test_a_typed_request_is_delivered_like_a_spoken_one(harness: Harness) -> None:
    with TestClient(harness.app) as client:
        for connection in _authed(client):
            connection.send_text(json.dumps({"t": "text", "text": "  merge the fold  "}))
            assert _until(connection, "utterance")["text"] == "merge the fold"
            assert _until(connection, "reply")["text"] == "done"
    assert harness.deliveries.texts == ["merge the fold"] and harness.transcribers == []


# --- always-listening, end to end -----------------------------------------------------------------


def test_listen_splits_two_utterances_on_silence_and_delivers_each() -> None:
    harness = Harness(canned=["spawn a coder", "and tell the manager"], mode="listen")
    with TestClient(harness.app) as client:
        for connection in _authed(client):
            for _ in range(FRAMES_PER_SECOND):
                connection.send_bytes(LOUD)
            interim = _until(connection, "stt")
            assert interim == {"t": "stt", "text": "spawn a coder", "final": False}
            for _ in range(FRAMES_OF_SILENCE):
                connection.send_bytes(QUIET)
            assert _until(connection, "stt")["text"] == "spawn a coder"
            assert _until(connection, "reply")["text"] == "done"
            for _ in range(FRAMES_PER_SECOND):
                connection.send_bytes(LOUD)
            for _ in range(FRAMES_OF_SILENCE):
                connection.send_bytes(QUIET)
            _until(connection, "stt")  # interim
            final = _until(connection, "stt")
            assert final == {"t": "stt", "text": "and tell the manager", "final": True}
            _until(connection, "reply")
    assert harness.deliveries.texts == ["spawn a coder", "and tell the manager"]
    assert harness.transcribers[0].finished == 2
    assert bytes(harness.transcribers[0].fed).count(LOUD) == 2 * FRAMES_PER_SECOND


def test_listen_the_stop_word_turns_the_mic_off_and_is_not_delivered() -> None:
    harness = Harness(canned="stop listening", mode="listen")
    with TestClient(harness.app) as client:
        for connection in _authed(client):
            for _ in range(FRAMES_PER_SECOND):
                connection.send_bytes(LOUD)
            for _ in range(FRAMES_OF_SILENCE):
                connection.send_bytes(QUIET)
            _until(connection, "stt")
            final = _until(connection, "stt")
            assert final["text"] == "stop listening" and final["final"]
            off = _until(connection, "listening")
            assert off == {"t": "listening", "on": False, "why": "stop word"}
            for _ in range(FRAMES_PER_SECOND):
                connection.send_bytes(LOUD)  # ignored: the mic is off
            connection.send_text(json.dumps({"t": "listen", "on": True}))
            assert _until(connection, "listening")["on"] is True
    assert harness.deliveries.texts == []


def test_a_typed_stop_word_turns_the_mic_off_and_frames_after_it_are_ignored() -> None:
    """coderp's minors: the stop word typed into the page's box ('Stop listening.') was delivered
    as a request, and nothing pinned that frames after the stop word go nowhere."""
    harness = Harness(canned="approve the deploy", mode="listen")
    with TestClient(harness.app) as client:
        for connection in _authed(client):
            connection.send_text(json.dumps({"t": "text", "text": "Stop listening."}))
            off = _until(connection, "listening")
            for _ in range(FRAMES_PER_SECOND):
                connection.send_bytes(LOUD)
            connection.send_text(
                json.dumps({"t": "text", "text": "ping"})
            )  # a typed request still lands
            reply = _until(connection, "reply")
    assert off == {"t": "listening", "on": False, "why": "stop word"}
    assert harness.deliveries.texts == ["ping"], "the stop word was not delivered, nor the frames"
    assert harness.transcribers == [], "no transcriber was ever fed after the mic went off"
    assert reply["text"] == "done"


def test_the_pages_own_mode_switch_is_not_bounced_back_by_the_key_poll() -> None:
    """coderp's minor: the 1 s poll read the key while the page's write was still in flight and
    flipped the page back. A slow key write must not undo the page's own switch."""
    harness = Harness(mode="focus")
    harness.mode_key = "focus"  # the key holds the OLD mode while the page's write is in flight
    slow_write = threading.Event()

    def set_mode_key(mode: voice.Mode) -> None:
        slow_write.wait(0.3)  # the write takes longer than several polls (poll_s is 0.02)
        harness.mode_key = mode
        harness.mode_writes.append(mode)

    harness.hooks = voice.Hooks(**{**harness.hooks.__dict__, "set_voice_mode": set_mode_key})
    harness.app = voice.build_app(token=TOKEN, hooks=harness.hooks, mode="focus")
    with TestClient(harness.app) as client:
        for connection in _authed(client):
            connection.send_text(json.dumps({"t": "mode", "mode": "listen"}))
            frames = [_until(connection, "mode")]
            deadline = time.monotonic() + 0.6
            while time.monotonic() < deadline:
                with contextlib.suppress(TimeoutError):
                    frames.append(_until_within(connection, "mode", 0.1))
    assert [f["mode"] for f in frames] == ["listen"], f"the page was bounced: {frames}"
    assert harness.mode_writes == ["listen"]


def _until_within(connection: Any, kind: str, seconds: float) -> dict[str, Any]:
    """Like ``_until``, bounded by ``seconds`` instead of the receive timeout."""
    received: list[str] = []

    def read() -> None:
        received.append(connection.receive_text())

    worker = threading.Thread(target=read, daemon=True)
    worker.start()
    worker.join(seconds)
    if not received:
        raise TimeoutError
    frame = json.loads(received[0])
    if frame["t"] != kind:
        return _until_within(connection, kind, seconds)
    return dict(frame)


def test_switching_modes_flushes_an_open_utterance_and_the_hello_says_which_mode() -> None:
    harness = Harness(canned="half a sentence", mode="listen")
    with TestClient(harness.app) as client, client.websocket_connect("/ws") as connection:
        connection.send_text(json.dumps({"t": "auth", "token": TOKEN}))
        hello = json.loads(_text(connection))
        assert hello["mode"] == "listen" and hello["listening"] is True
        assert set(hello) == {"t", "mode", "listening", "speaker", "thinking"}
        for _ in range(FRAMES_PER_SECOND):
            connection.send_bytes(LOUD)
        _until(connection, "stt")
        connection.send_text(json.dumps({"t": "mode", "mode": "focus"}))
        final = _until(connection, "stt")
        assert final["text"] == "half a sentence" and final["final"]
        # The switch lands at once (coderp's S1): the mode frame does not wait behind the
        # flushed utterance's turn, which answers after it.
        assert _until(connection, "mode") == {"t": "mode", "mode": "focus", "listening": False}
        _until(connection, "reply")
        connection.send_text(json.dumps({"t": "mode", "mode": "sideways"}))
        assert _until(connection, "error")["code"] == "bad_message"
    assert harness.deliveries.texts == ["half a sentence"]


# --- the frame around it --------------------------------------------------------------------------


def test_the_mode_key_is_the_default_for_a_new_page_and_the_pages_toggle_writes_it() -> None:
    """13179: state.json captain_voice_mode is the mode's single home."""
    harness = Harness(mode="focus")
    harness.mode_key = "listen"  # T4's control, or an earlier page, set it
    with TestClient(harness.app) as client, client.websocket_connect("/ws") as connection:
        connection.send_text(json.dumps({"t": "auth", "token": TOKEN}))
        hello = json.loads(_text(connection))
        assert (hello["t"], hello["mode"], hello["listening"]) == ("hello", "listen", True), (
            "the key wins over the server's own default"
        )
        connection.send_text(json.dumps({"t": "mode", "mode": "focus"}))
        assert _until(connection, "mode") == {"t": "mode", "mode": "focus", "listening": False}
    assert harness.mode_writes == ["focus"], "the page's toggle wrote the key"


def test_a_change_of_the_mode_key_switches_a_connected_page() -> None:
    """13179's pin: T4's toggle (or another page) writes the key; this page follows within a poll
    and is told, so its mic opens — and the switch that came from the key is not written back."""
    harness = Harness(mode="focus")
    with TestClient(harness.app) as client, client.websocket_connect("/ws") as connection:
        connection.send_text(json.dumps({"t": "auth", "token": TOKEN}))
        assert json.loads(_text(connection))["mode"] == "focus"
        harness.mode_key = "listen"
        switched = _until(connection, "mode")
    assert switched == {"t": "mode", "mode": "listen", "listening": True}
    assert harness.mode_writes == [], "a switch that came from the key is not written back"


def test_the_mode_key_helpers_read_and_write_state_json_and_say_garbage(
    isolated_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from aisquare.core import state_file

    assert voice.voice_mode() is None, "never set: no default is invented here"
    voice.set_voice_mode("listen")
    assert voice.voice_mode() == "listen"
    assert state_file.read_state()[voice.MODE_STATE_KEY] == "listen", "a plain string, for T4"
    with pytest.raises(ValueError, match=r"mode must be one of"):
        voice.set_voice_mode("shout")  # type: ignore[arg-type]
    state_file.update_state(voice.MODE_STATE_KEY, {"mode": "listen"})
    with caplog.at_level(logging.WARNING, logger="aisquare.services.captain.voice"):
        assert voice.voice_mode() is None
    assert any("captain_voice_mode" in r.getMessage() for r in caplog.records), "said, not a mode"


def test_a_wrong_token_is_refused_and_closed_and_a_binary_first_frame_too(harness: Harness) -> None:
    from starlette.websockets import WebSocketDisconnect

    with TestClient(harness.app) as client:
        with client.websocket_connect("/ws") as connection:
            connection.send_text(json.dumps({"t": "auth", "token": "wrong"}))
            error = json.loads(_text(connection))
            assert error == {
                "t": "error",
                "code": "auth_failed",
                "message": "the token was rejected",
            }
            with pytest.raises(WebSocketDisconnect) as closed:
                connection.receive_text()
            assert closed.value.code == voice.CLOSE_AUTH_FAILED
        with client.websocket_connect("/ws") as connection:
            connection.send_bytes(LOUD)
            assert json.loads(_text(connection))["code"] == "auth_invalid"


def test_the_speaker_switch_is_on_the_wire_and_the_page_runs_no_spool_drainer(
    harness: Harness,
) -> None:
    """13143 (5): exactly ONE drainer of the speech spool, in the captain's server process
    (``speaker.start_drainer`` from ``actions.run_stdio``); the page server has none."""
    with TestClient(harness.app) as client:
        for connection in _authed(client):
            connection.send_text(json.dumps({"t": "speaker", "on": False}))
            assert _until(connection, "speaker") == {"t": "speaker", "on": False}
    assert speaker_mod.speaker_on() is False
    assert harness.spoken.lines == []
    assert not hasattr(voice._Connection, "_spool_loop")
    assert "take_speech" not in Hooks.__dataclass_fields__
    assert not hasattr(voice, "SPOOL_POLL_S")


def test_spoke_since_counts_only_ok_speak_audits_on_the_home_board(isolated_home: Path) -> None:
    from aisquare.services import team as team_service
    from aisquare.services.captain import actions

    home = captain_state.home_project()
    before = voice.home_seq()
    actions.speak("the fold is green", "how is the fold")  # one ok speak, audited by T1
    session = captain_state.ensure_session(home)
    for text in (
        json.dumps({"v": 1, "tool": "speak", "ok": False, "said": "nothing to say"}),
        json.dumps({"v": 1, "tool": "thinking", "ok": True, "said": "on"}),
        "not json at all",
    ):
        team_service.add_note(text, session_ref=session, kind="captain_action")
    assert voice.spoke_since(before) == 1
    assert voice.spoke_since(voice.home_seq()) == 0, "a speak() before a turn is not the turn's"
    # coderp's S3: the window opens when the text is typed. A speak() audited before that
    # moment (the busy turn's, another page's) is not this turn's, seq or no seq.
    from datetime import timedelta

    later = datetime.now(tz=UTC) + timedelta(seconds=5)
    assert voice.spoke_since(before, later) == 0, "spoken before the text went in"
    assert voice.spoke_since(before, later - timedelta(minutes=1)) == 1


def test_an_unavailable_backend_is_said_with_its_fix_not_a_dead_socket() -> None:
    def broken() -> voice.Transcriber:
        raise voice.SpeechUnavailable("faster-whisper is not installed", voice.INSTALL_FIX)

    harness = Harness()
    harness.hooks.transcriber_factory = broken
    with TestClient(harness.app) as client:
        for connection in _authed(client):
            connection.send_text(json.dumps({"t": "audio", "seq": 1}))
            error = _until(connection, "error")
            assert error["code"] == "stt_unavailable" and "[voice]" in error["fix"]
            connection.send_text(json.dumps({"t": "text", "text": "typing still works"}))
            assert _until(connection, "reply")["text"] == "done"


def test_the_page_is_served_from_the_package_and_carries_the_worklet(harness: Harness) -> None:
    with TestClient(harness.app) as client:
        response = client.get("/")
    assert response.status_code == 200 and "text/html" in response.headers["content-type"]
    body = response.text
    assert "registerProcessor('pcm-framer'" in body and "hold to talk" in body
    assert body.encode("utf-8") == voice.page_bytes()


def test_the_url_the_adb_line_and_the_qr() -> None:
    url = voice.voice_url(8749, "abc")
    assert url == "http://localhost:8749/#token=abc"
    assert voice.adb_reverse(8749) == "adb reverse tcp:8749 tcp:8749"
    lines = voice.qr_lines(url)
    assert (
        lines is not None and len(lines) > 10 and all(len(line) == len(lines[0]) for line in lines)
    )


def test_without_segno_the_qr_is_none_and_the_url_still_prints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "segno", None)
    assert voice.qr_lines("http://localhost:8749/#token=x") is None


def test_the_real_transcriber_refuses_an_unknown_model_and_names_the_extra_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(voice.SpeechUnavailable, match=r"base\.en, small\.en"):
        voice.transcriber("large-v3")
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    monkeypatch.setitem(sys.modules, "numpy", None)
    with pytest.raises(voice.SpeechUnavailable) as caught:
        voice.transcriber()
    assert caught.value.fix == voice.INSTALL_FIX and "[voice]" in caught.value.fix


def test_the_default_hooks_reach_the_product_seams() -> None:
    hooks = Hooks()
    assert hooks.deliver is voice.deliver_to_captain
    assert hooks.thinking is voice.captain_is_thinking
    assert hooks.voice_mode is voice.voice_mode and hooks.set_voice_mode is voice.set_voice_mode
    assert hooks.home_seq is voice.home_seq and hooks.spoke_since is voice.spoke_since
    assert hooks.on_thinking is None, "the CLI wires its terminal in; the library prints nothing"
    assert isinstance(hooks.voice, speaker_mod.Voice)


def test_the_default_voice_is_the_configured_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """coderp's S5: the page built its own Voice from the platform adapter and ignored
    [captain] speaker, so the page and the drainer spoke through different adapters."""
    monkeypatch.setattr(speaker_mod, "configured_speaker", lambda: "null")
    assert isinstance(Hooks().voice.speaker, speaker_mod.NullSpeaker)


def test_captain_is_thinking_reads_the_busy_flag_first(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(brain, "find", lambda: None)
    assert voice.captain_is_thinking() is False
    captain_state.set_busy(True)
    assert voice.captain_is_thinking() is True
