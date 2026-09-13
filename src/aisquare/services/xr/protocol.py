"""Every message on the cliXR wire, as a Pydantic v2 model.

This module is the contract. A browser client written against
``web/xr/protocol.schema.json`` and this server are two implementations of the
same document, built by different people at the same time, and the only thing
keeping them honest is that the schema is generated from these models and
committed::

    python -m aisquare.services.xr.protocol --write    # regenerate
    python -m aisquare.services.xr.protocol --check    # non-zero on drift

``tests/test_xr_protocol.py`` runs ``--check``'s comparison, so a field renamed
here and not regenerated fails the suite rather than a headset.

Field names are ``camelCase`` on the wire because the other half is JavaScript;
they are ``snake_case`` in Python where the two differ, and the alias is what
serializes. ``for`` is the one field that cannot share its Python name at all
(it is a keyword), so :class:`Ack` spells it ``for_`` and aliases it back.

:data:`PROTOCOL_VERSION` goes out in every ``hello``. Bump it when a change
would make an older client misread a frame — not for an added optional field,
which both sides tolerate by construction.

**The binary frames are part of this contract too.** Everything above is JSON,
and for JSON the generated schema is the whole story. Audio is not: between an
``audio`` header and its ``audioEnd`` the client sends RAW SAMPLES as binary
websocket messages, and a format that lives only in the two implementations is
a format that is gone the day either author is. The encoding is therefore fixed
here, carried into the schema as the ``audio`` block by :func:`schema_document`,
and stated once in full:

``pcm_s16le`` — 16 kHz, mono, signed 16-bit PCM, little-endian, no container
and no header of any kind. One frame is 20 ms: 320 samples, 640 bytes. A frame
of an odd byte length is half a sample and there is no way to interpret it —
every sample after it is shifted by eight bits — so the sender's buffer must be
sample-aligned by construction.

These are not aspirational numbers. They are what
:mod:`aisquare.services.xr.speech` decodes (``SAMPLE_RATE``/``SAMPLE_BYTES``)
and what the client's ``AudioWorklet`` emits, and the cap on one burst
(``server.MAX_AUDIO_BYTES``) is derived from them rather than guessed at.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

PROTOCOL_VERSION = 1
"""Wire version announced in ``hello``. See the module docstring for when to bump."""

AUDIO_ENCODING = "pcm_s16le"
"""Encoding of every binary frame between ``audio`` and ``audioEnd``."""

AUDIO_SAMPLE_RATE_HZ = 16_000
"""Sample rate of :data:`AUDIO_ENCODING`. What the speech backend decodes at."""

AUDIO_SAMPLE_BITS = 16
"""Bits per sample: signed, little-endian, two bytes."""

AUDIO_CHANNELS = 1
"""Mono. A headset microphone array is downmixed by the client, not here."""

AUDIO_FRAME_MS = 20
"""Nominal frame duration: 320 samples, 640 bytes. The server buffers whatever
arrives, so this is the shape to send rather than a length it enforces."""

CLOSE_AUTH_FAILED = 4401
"""Websocket close code for a rejected token: do not retry with this one.

In the private 4000-4999 range, and stated in the CONTRACT rather than only in
the server because the client's reconnect policy turns on it. Every other close
this server can produce is a transport close, where reconnecting with backoff
is correct; this is the one case where it is wrong, because the token will be
just as wrong the next time. A client that cannot tell the two apart from the
schema has to guess, and the guess that costs its author nothing to write is
the one that spins forever against a server that will never accept it.
"""

SessionState = Literal["working", "waiting", "needs_you", "gone"]
"""What the operator needs to know about a session at a glance.

``gone`` is in the vocabulary because the client may hold a panel that has
aged out; the projector itself reports departures as ``delta.removed`` and
never emits a session in this state (:func:`projector.classify`).
"""

SessionRole = Literal["planner", "coder", "runner", "remote"]
"""The role bucket a panel is labelled with. ``remote`` is an MCP client."""

ColorKey = Literal["planner", "coder", "runner"]
"""Palette slot. The client maps this to hex; the server never sends colour."""


class _Wire(BaseModel):
    """Shared config: aliases populate both ways, unknown fields are refused.

    ``extra="forbid"`` is deliberate on a protocol that two people are
    implementing in parallel. A client that sends ``{"t": "prompt", "session":
    …, "message": …}`` when the field is called ``text`` should be told so on
    the first frame, not silently prompt an agent with an empty string.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


# --- entities -------------------------------------------------------------------


class Session(_Wire):
    """One agent session as a panel in the ring.

    ``summary`` is computed by the server from the session's most recent BOARD
    EVENT and is capped at six words. It is never derived from transcript
    text — the ambient tier must stay glanceable, and transcript content
    reaches the client only for the one session it has subscribed to.
    """

    id: str
    role: SessionRole
    title: str
    """Short and human: the claimed task's title, the session's focus, or its label."""
    state: SessionState
    summary: str
    """<= 6 words, from the latest board event. Never transcript text."""
    task_id: str | None = Field(default=None, alias="taskId")
    color_key: ColorKey = Field(alias="colorKey")
    last_activity_at: str = Field(alias="lastActivityAt")
    """ISO 8601. A string on the wire so the client parses it once, its way."""
    unread: int = 0
    """Board events for this session since this connection last subscribed to it."""


class Task(_Wire):
    """A board task, for the client's task-side affordances."""

    id: str
    title: str
    status: str
    role: str | None = None
    claimed_by: str | None = Field(default=None, alias="claimedBy")


class Group(_Wire):
    """A visual container for several panels. Reserved: ``groups`` is ``[]`` today."""

    id: str
    title: str
    color_key: ColorKey = Field(alias="colorKey")
    sessions: list[str] = Field(default_factory=list)


# --- server -> client -----------------------------------------------------------


class Hello(_Wire):
    """First frame after a successful auth."""

    t: Literal["hello"] = "hello"
    protocol: int = PROTOCOL_VERSION
    hub: str
    """The board this socket is attached to: the project id."""
    server_time: str = Field(alias="serverTime")


class Snapshot(_Wire):
    """The whole board. Sent once after ``hello``, and again on reconnect."""

    t: Literal["snapshot"] = "snapshot"
    sessions: list[Session] = Field(default_factory=list)
    tasks: list[Task] = Field(default_factory=list)
    groups: list[Group] = Field(default_factory=list)


class Delta(_Wire):
    """What changed since the last frame. Never sent empty."""

    t: Literal["delta"] = "delta"
    changed: list[Session] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    """Session ids that ended or went stale. Sent once, on the transition."""


class Transcript(_Wire):
    """A chunk of the subscribed session's transcript.

    ``seq`` increases per subscription, so a client that misses a frame knows.
    ``final`` marks the end of one transcript record, not the end of the stream.
    """

    t: Literal["transcript"] = "transcript"
    session: str
    seq: int
    text: str
    final: bool = True


class Stt(_Wire):
    """A speech-to-text result: interim (``final=False``) then final."""

    t: Literal["stt"] = "stt"
    text: str
    final: bool = False


class Error(_Wire):
    """Something the client asked for did not happen, and why.

    ``code`` is the stable half — key on it. ``message`` is for a human
    reading a console.
    """

    t: Literal["error"] = "error"
    code: str
    message: str


class Ack(_Wire):
    """The outcome of a client action.

    The one addition to the protocol as drafted, and the reason is
    :func:`services.fleet.tell`: a prompt is delivered either by typing into a
    waiting pane or by filing a board note for the agent's next delta, and
    which of the two happened is something the operator wants to see. Without
    this frame a prompt that became a note is indistinguishable from one that
    landed in the pane.
    """

    t: Literal["ack"] = "ack"
    for_: Literal["prompt"] = Field(default="prompt", alias="for")
    session: str
    ok: bool
    detail: str = ""


# --- client -> server -----------------------------------------------------------


class Auth(_Wire):
    """Always the first frame. The socket is closed if it is not."""

    t: Literal["auth"] = "auth"
    token: str


class Subscribe(_Wire):
    """Start (or with ``session=None``, stop) streaming one transcript."""

    t: Literal["subscribe"] = "subscribe"
    session: str | None = None


class Prompt(_Wire):
    """Send text to one session, as the operator."""

    t: Literal["prompt"] = "prompt"
    session: str
    text: str


class Audio(_Wire):
    """Header for a push-to-talk burst. Binary frames follow until ``audioEnd``.

    Those frames are raw ``pcm_s16le`` — 16 kHz, mono, signed 16-bit
    little-endian PCM, no container — and never anything else. The module
    docstring states it in full and :func:`schema_document` publishes the
    numbers as the ``audio`` block, so a client author never has to open this
    file or infer the format from a comment about a byte cap.
    """

    t: Literal["audio"] = "audio"
    session: str = Field(
        description=(
            "Session this burst is addressed to. The HEADER owns the burst: the "
            "matching audioEnd must name the same session, and the server "
            "refuses the burst if it does not, rather than attributing the "
            "operator's speech to whichever of the two frames it happened to "
            "read last."
        )
    )
    seq: int = Field(
        default=0,
        ge=0,
        description=(
            "Ordinal of this burst on this connection; 0 for a client that does "
            "not number its bursts. It is NOT a per-chunk sequence number: the "
            "binary frames carry no sequencing and there is no gap detection "
            "anywhere in this protocol, because a websocket delivers its "
            "messages in order or not at all. The server reads this only to "
            "reject a negative value."
        ),
    )


class AudioEnd(_Wire):
    """End of a push-to-talk burst: transcribe what was buffered."""

    t: Literal["audioEnd"] = "audioEnd"
    session: str = Field(
        description=(
            "Must equal the session on the audio header that opened this burst. "
            "A mismatch is answered with a session_mismatch error and the audio "
            "is discarded."
        )
    )


ServerMessage = Annotated[
    Hello | Snapshot | Delta | Transcript | Stt | Error | Ack,
    Field(discriminator="t"),
]
ClientMessage = Annotated[
    Auth | Subscribe | Prompt | Audio | AudioEnd,
    Field(discriminator="t"),
]

_SERVER = TypeAdapter[Any](ServerMessage)
_CLIENT = TypeAdapter[Any](ClientMessage)


def parse_client(text: str) -> Any:
    """One client frame as its model.

    Raises ``pydantic.ValidationError`` for a frame that is not one of the
    five client messages, and ``ValueError`` for text that is not JSON at all
    — the caller turns both into an ``error`` frame rather than a traceback.
    """
    return _CLIENT.validate_json(text)


def to_wire(message: Any) -> str:
    """A server message as the JSON string to put on the socket.

    ``by_alias`` is what makes ``for_`` serialize as ``for`` and ``taskId``
    come out camelCase; every model here goes out through this one function so
    a frame can never be dumped the other way by accident.
    """
    return json.dumps(message.model_dump(by_alias=True, mode="json"), separators=(",", ":"))


# --- schema ---------------------------------------------------------------------


def schema_document() -> dict[str, Any]:
    """The committed schema: both directions of the protocol in one file.

    ``serialization`` mode for the server half and ``validation`` for the
    client half, because those are the two questions the client actually asks
    of it — "what will I receive" and "what may I send" — and they differ
    (a field with a default is required in neither, but only the validation
    schema says so).

    ``audio`` and ``closeCodes`` are hand-built blocks rather than generated
    ones, because neither is a JSON frame and JSON Schema can describe neither:
    a binary websocket message has no schema, and a close code is not a message
    at all. They are here anyway, because a contract that covers only the part
    that generates easily is a contract with a hole in it exactly where the
    second implementer has to guess.
    """
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "cliXR wire protocol",
        "description": (
            "Generated from aisquare.services.xr.protocol — do not edit. "
            "Regenerate with: python -m aisquare.services.xr.protocol --write"
        ),
        "protocol": PROTOCOL_VERSION,
        "audio": {
            "description": (
                "Binary websocket frames between an `audio` header and its "
                "`audioEnd` are raw samples in this format, with no container "
                "and no per-frame header. JSON Schema cannot describe a binary "
                "frame, so it is stated here: this block IS the contract for "
                "the audio half."
            ),
            "encoding": AUDIO_ENCODING,
            "sampleRateHz": AUDIO_SAMPLE_RATE_HZ,
            "sampleBits": AUDIO_SAMPLE_BITS,
            "signed": True,
            "endianness": "little",
            "channels": AUDIO_CHANNELS,
            "frameMs": AUDIO_FRAME_MS,
            "frameBytes": AUDIO_SAMPLE_RATE_HZ * (AUDIO_SAMPLE_BITS // 8) * AUDIO_FRAME_MS // 1000,
            "alignment": (
                "Every frame must be a whole number of samples. An odd byte "
                "length is half a sample and shifts every sample after it by "
                "eight bits."
            ),
        },
        "closeCodes": {
            str(CLOSE_AUTH_FAILED): {
                "name": "CLOSE_AUTH_FAILED",
                "retry": False,
                "description": (
                    "The token was rejected. Do not reconnect with it — it will "
                    "be rejected again. Every close code NOT listed here is a "
                    "transport close, where reconnecting with backoff is right."
                ),
            }
        },
        "server": _SERVER.json_schema(by_alias=True, mode="serialization"),
        "client": _CLIENT.json_schema(by_alias=True, mode="validation"),
    }


def schema_text() -> str:
    """:func:`schema_document` as the exact bytes that belong in the file."""
    return json.dumps(schema_document(), indent=2) + "\n"


def schema_path() -> Any:
    """Where the committed schema lives, as an importlib Traversable.

    Package data, not ``__file__`` arithmetic: the server serves this same
    file to the browser out of an installed wheel, where the source tree the
    ``--write`` path writes into does not exist.
    """
    from importlib.resources import files

    return files("aisquare") / "web" / "xr" / "protocol.schema.json"


def _main(argv: list[str] | None = None) -> int:
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__, prog="aisquare.services.xr.protocol")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="Regenerate the committed schema.")
    group.add_argument("--check", action="store_true", help="Exit non-zero if it has drifted.")
    args = parser.parse_args(argv)

    wanted = schema_text()
    target = Path(str(schema_path()))
    if args.write:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(wanted, encoding="utf-8")
        print(f"wrote {target}")
        return 0
    current = target.read_text(encoding="utf-8") if target.exists() else ""
    if current == wanted:
        print(f"{target} is current (protocol {PROTOCOL_VERSION})")
        return 0
    print(
        f"{target} has drifted from the models in {__name__}.\n"
        "Regenerate it: python -m aisquare.services.xr.protocol --write",
    )
    return 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(_main())
