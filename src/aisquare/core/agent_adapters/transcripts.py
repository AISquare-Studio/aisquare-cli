"""What every agent's transcript reader shares: the facts the needs-you scan classifies on,
and the byte and terminal helpers each reader and ``services.transcript`` use.

An agent's own format is its adapter's to read (:attr:`AgentAdapter.transcript_reader
<aisquare.core.agent_adapters.types.AgentAdapter.transcript_reader>`, an
:class:`~aisquare.core.agent_adapters.types.AgentTranscript`): Claude Code's JSONL in
``claude_transcript``, Codex's rollout in ``codex_transcript``. ``services.transcript``
opens the file, hands it to the row's reader, and pages what it renders.

Two properties shape everything here. **The files are large**: 1.4 to 18 MB measured, and
one pasted attachment or tool output can be megabytes on a SINGLE line, so a file is read
BACKWARDS from its end in blocks (:func:`lines_backwards`), never whole, within a byte
budget, and a line past :data:`MAX_LINE` is never handed to ``json.loads``. **The server
owns the rendering**: the lines go into the same terminal the live screen paints into, so
they are terminal-ready, wrapped in the phone's own columns (:func:`wrap`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.cells import split_graphemes

MAX_LINE = 262_144
"""Longest JSONL line that is parsed at all.

Attachments and tool results can be megabytes on one line. They render to
nothing anyway, so they are skipped WITHOUT being handed to ``json.loads`` —
otherwise the cost of ignoring a 10 MB paste would be parsing it first. A reader
reads such a line for its ids alone, off its bytes.
"""

CHUNK = 65_536

TAIL_BUDGET = 524_288
"""Most bytes a tail read (``services.transcript.read_transcript_tail``) reads off the end
of a transcript."""

TAIL_RECORDS = 400
"""Most conversation records a tail walk examines: a backstop, not the usual bound."""

TOOL_INPUT_MAX = 16_384
"""Most bytes of a pending tool's input (as JSON) a :class:`PendingTool` keeps; more is ``{}``."""


# --- the tail: what the needs-you scan classifies on (SPEC §4.3) ---------------------------


@dataclass(frozen=True)
class PendingTool:
    """A tool use of the newest assistant message that has no result yet."""

    tool_use_id: str
    name: str
    summary: str
    """The transcript's own one-line form of the call, ``Bash(git push --force)``."""
    input: Mapping[str, object]
    """The call's input, kept only when its JSON is at most :data:`TOOL_INPUT_MAX` bytes;
    ``{}`` otherwise (a ``Write`` of a whole file is not what a phone reads to decide)."""
    at: datetime | None
    input_dropped: bool = False
    """The call has an input that ``input`` leaves out: over :data:`TOOL_INPUT_MAX`, or on a
    line too long to parse. Its card says so: showing nothing of it, it read as a call with
    none, and a "1" on the phone approved it blind."""


@dataclass(frozen=True)
class TranscriptTail:
    """The end of one conversation, reduced to what decides whether it needs the human."""

    pending: tuple[PendingTool, ...]
    """Tool uses still waiting for their result, OLDEST first."""
    newest: str
    """The newest record: ``assistant_text``, ``assistant_tool``, ``user_prompt``,
    ``tool_result``, ``interrupted`` (an Esc, or a rejected tool use), ``own`` (one the
    agent wrote itself: a local command and its output, a compaction's summary) or
    ``none``."""
    newest_at: datetime | None
    last_text: str | None
    """The newest assistant message's text blocks, joined; ``None`` when it has none."""
    last_text_at: datetime | None
    marker_key: str | None
    """The ``uuid`` of the record that decided ``newest``, else its byte offset."""
    empty: bool = False
    """The file has nothing in it yet. ``newest`` is also ``none`` for a walk that met no
    conversation record within :data:`TAIL_RECORDS` or its budget (only other kinds of
    record, or lines it could not read): that one says nothing about what was written."""
    cut_at: datetime | None = None
    """When the file was last written, for a walk that ran out of its budget or of
    :data:`TAIL_RECORDS` before it had read the newest assistant message whole (for
    records without ids, back to the human's prompt), on records that do not end a turn:
    a tool use of that message went unseen, and may still wait on its result, its
    permission prompt up. A screenshot's result of 700 KB, written while a sibling call
    waits on its prompt, is such a walk: the budget ends inside it, ``pending`` is empty,
    and the agent read as one at rest (review of #243, round 7). So is one that met a
    later block of the message first, as tools run while it streams. ``None`` when the
    walk saw what it needed, or saw the newest message say only text, or its turn end,
    which ends a turn."""
    under_own: str = "none"
    """When ``newest`` is ``own``: the newest record that is not, in ``newest``'s words, or
    ``none`` when the walk met none. A local command or a ``!`` command typed at the
    agent's prompt runs no turn and fires no hook, so the record under it still says where
    the agent is: at an interruption, at its own words. Read as the newest, such records
    hid an interruption, and a session still ``attention`` after a prompt answered with
    Esc read as a dialog up (sweep 5 of #243)."""
    under_own_at: datetime | None = None
    under_own_key: str | None = None
    """That record's time, and its ``uuid`` (else its byte offset), as ``newest_at`` and
    ``marker_key`` are the newest's."""


TAIL_NOTHING = TranscriptTail(
    pending=(),
    newest="none",
    newest_at=None,
    last_text=None,
    last_text_at=None,
    marker_key=None,
    empty=True,
)
"""The tail of a transcript with no record in it yet."""


def lines_backwards(file: Path, end: int, *, budget: int) -> Iterator[tuple[int, bytes]]:
    """``(offset, line)`` newest first, reading only the tail that is needed.

    ``offset`` is where the line starts, which is exactly what a later ``before``
    needs in order to continue from here without re-reading or skipping a turn.

    At most ``budget`` bytes are read. When that runs out inside a line already
    longer than :data:`MAX_LINE`, the part read so far is yielded too, at the
    offset the read reached: every caller skips such a line unparsed, so one that
    resumes from the oldest offset it was handed moves past the line instead of
    reading the same budget of it again. A shorter line the budget cut is not
    yielded, and resuming from the last whole line reads it whole. With a budget
    over :data:`MAX_LINE`, a read that does not reach the start of the file
    therefore always yields an offset before ``end``.
    """
    with file.open("rb") as handle:
        position = end
        pending = b""
        scanned = 0
        while position > 0:
            if scanned >= budget:
                if len(pending) > MAX_LINE:
                    yield position, pending
                return
            size = min(CHUNK, position)
            position -= size
            scanned += size
            handle.seek(position)
            pending = handle.read(size) + pending
            while True:
                newline = pending.rfind(b"\n")
                if newline == -1:
                    break
                line = pending[newline + 1 :]
                pending = pending[:newline]
                if line.strip():
                    yield position + newline + 1, line
        if pending.strip():
            yield 0, pending


def parse_line(raw: bytes) -> dict[str, Any] | None:
    """One JSONL record, or ``None`` for anything that cannot be one usefully.

    A line nested deeper than ``json`` recurses (well inside :data:`MAX_LINE`:
    about 1 000 levels on 3.11) raises ``RecursionError``, not ``ValueError``;
    it is no record either, and must not cost the page or the tail its reader.
    """
    if len(raw) > MAX_LINE:
        return None
    try:
        record = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return record if isinstance(record, dict) else None


def one_per_line(lines: Iterable[tuple[int, bytes]]) -> Iterator[tuple[int, dict[str, Any] | None]]:
    """``AgentTranscript.records`` for a transcript of one whole record a line (Claude Code's
    session JSONL, Codex's rollout): each line's :func:`parse_line`, at its offset."""
    for offset, raw in lines:
        yield offset, parse_line(raw)


def record_time(record: Mapping[str, Any]) -> datetime | None:
    """The record's ``timestamp`` as an aware datetime, a time without a zone being UTC (both
    agents write ``Z``); ``None`` when it has none."""
    raw = record.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def kept_input(payload: object) -> tuple[Mapping[str, object], bool]:
    """A call's input as a :class:`PendingTool` keeps it, and whether any of it was left
    out: an object of at most :data:`TOOL_INPUT_MAX` bytes as JSON is kept whole, a larger
    one (or one too deep to write back) is ``{}``, dropped."""
    if not isinstance(payload, dict):
        return {}, False
    try:
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8", "replace"))
    except (ValueError, RecursionError):  # what parsed may still be too deep to write
        size = TOOL_INPUT_MAX + 1
    return (payload if size <= TOOL_INPUT_MAX else {}), size > TOOL_INPUT_MAX


# --- terminal lines: what a page renders a record to -----------------------------------------


DIM = "\x1b[2m"
OFF = "\x1b[0m"
YOU = "\x1b[1;36m> you\x1b[0m"
"""The human's turns, as the terminal shows them. Plain ASCII markers on purpose: the
lines land in someone's terminal font, and a glyph that renders as a box on one phone is
worse than a character everything has."""


def speaker_line(speaker: str) -> str:
    """The line an agent's own turn starts with, ``* <speaker>``, as :data:`YOU` is the
    human's (:attr:`AgentTranscript.speaker
    <aisquare.core.agent_adapters.types.AgentTranscript.speaker>`)."""
    return f"\x1b[1;32m* {speaker}\x1b[0m"


_BREAK = re.compile(r"([\t\n\x0b\x0c\r ]+)")
"""Where a line may break: a run of the whitespace ``textwrap`` breaks at."""


def cells(text: str) -> int:
    """How many columns ``text`` takes on a terminal, as rich draws it: two for a wide
    character (CJK, most emoji), none for a mark that combines with the one before."""
    return split_graphemes(text)[1]


def _fit_cells(text: str, room: int) -> int:
    """How many characters of ``text``, whole graphemes, fit in ``room`` columns."""
    used = fit = 0
    for _start, end, width in split_graphemes(text)[0]:
        if used + width > room:
            break
        used, fit = used + width, end
    return fit


def cut_cells(text: str, room: int) -> str:
    """``text`` in at most ``room`` columns: itself, or its start and ``…``."""
    if cells(text) <= room:
        return text
    return text[: _fit_cells(text, max(0, room - 1))] + "…"


def wrap(text: str, width: int, *, indent: str = "  ") -> list[str]:
    """``text`` as lines of at most ``width`` columns, each after ``indent``, its paragraphs
    kept: a run of whitespace is where a line breaks, and a word longer than a line breaks
    where it reaches the end.

    Measured in columns, as the page counts the width it asks for (``?width=``), not in
    characters: ``textwrap`` counted characters, so a line of Japanese wrapped at 40 took
    78 columns, and the phone wrapped each again into a full row and a ragged half outside
    the indent (review of #243, sweep 5).
    """
    room = max(1, width - cells(indent))
    out: list[str] = []
    for paragraph in text.replace("\r\n", "\n").split("\n"):
        stripped = paragraph.rstrip().expandtabs()
        if not stripped:
            out.append("")
            continue
        out.extend(indent + line for line in _wrap_paragraph(stripped, room))
    return out


def _wrap_paragraph(text: str, room: int) -> list[str]:
    """``textwrap``'s greedy fill of one paragraph, its lengths in columns: whitespace is
    dropped where a line begins or ends, except the paragraph's own leading run."""
    chunks = [(chunk, cells(chunk)) for chunk in _BREAK.split(text) if chunk]
    chunks.reverse()
    lines: list[str] = []

    def wrapped_line(line: list[str]) -> None:
        while line and not line[-1].strip():
            line.pop()
        if line:
            lines.append("".join(line))

    while chunks:
        if lines and not chunks[-1][0].strip():
            chunks.pop()
            continue
        line: list[str] = []
        used = 0
        while chunks and used + chunks[-1][1] <= room:
            chunk, width = chunks.pop()
            line.append(chunk)
            used += width
        if chunks and chunks[-1][1] > room:
            # A word longer than a line: the rest of this line, each whole line it fills,
            # and what is left of it, a word of its own.
            head, *rest = _cell_pieces(chunks.pop()[0], room - used, room, bool(line))
            line.append(head[0])
            for piece, _width in rest[:-1]:
                wrapped_line(line)
                line = [piece]
            if rest:
                chunks.append(rest[-1])
        wrapped_line(line)
    return lines


def _cell_pieces(
    word: str, first: int, room: int, first_may_be_empty: bool
) -> list[tuple[str, int]]:
    """``word`` cut between graphemes into pieces and their widths: the first of at most
    ``first`` columns, each other of at most ``room``. A piece holds at least one grapheme,
    the first only when ``first_may_be_empty`` is false, so a wide character on a line of
    one column still moves on. Measured once, whatever its length."""
    pieces: list[tuple[str, int]] = []
    start = used = 0
    limit = first
    for at, _end, width in split_graphemes(word)[0]:
        if used + width > limit and (used or (first_may_be_empty and not pieces)):
            pieces.append((word[start:at], used))
            start, used, limit = at, 0, room
        used += width
    pieces.append((word[start:], used))
    return pieces


def dim_note(text: str, width: int) -> str:
    """One dim ``  ⎿`` line that fits ``width`` columns: ``text``, or its start and ``…``.

    A tool call's summary went as long as 82 columns whatever the width asked for, and a
    note was cut by characters, as :func:`wrap` once measured: either took two rows of
    a phone's (review of #243, sweep 5).
    """
    return f"{DIM}  ⎿ {cut_cells(text, max(1, width - 4))}{OFF}"


def question_lines(questions: object, width: int) -> list[str]:
    """Questions an agent asks the human (Claude Code's ``AskUserQuestion``, Codex's
    ``request_user_input``: ``header``, ``question``, ``options`` of ``label`` and
    ``description``) as the human must read them to answer.

    One line per question, ``? <header>: <question>``, then its options numbered
    as the dialog numbers them, with what each means: the one-line summary
    showed only that a question had been asked, which is the one thing a phone
    deciding the answer from here already knew.
    """
    lines: list[str] = []
    for question in questions if isinstance(questions, list) else []:
        if not isinstance(question, dict):
            continue
        header = question.get("header")
        text = question.get("question")
        asked = text if isinstance(text, str) else ""
        line = f"? {header}: {asked}" if isinstance(header, str) and header else f"? {asked}"
        if question.get("multiSelect") is True:
            line += " (multi-select)"
        lines.extend(wrap(line, width))
        options = question.get("options")
        number = 0
        for option in options if isinstance(options, list) else []:
            if not isinstance(option, dict):
                continue
            number += 1
            label = option.get("label")
            meaning = option.get("description")
            entry = f"{number}. {label if isinstance(label, str) else ''}"
            if isinstance(meaning, str) and meaning.strip():
                entry += f" — {meaning}"
            lines.extend(wrap(entry, width, indent="    "))
    return lines


def first_question(arguments: Mapping[str, object]) -> str:
    """The words of the first question a call's input asks (``arguments["questions"]``), or
    ``""``: what its dialog shows of it, to tell its prompt on a screen."""
    questions = arguments.get("questions")
    asked = (q for q in (questions if isinstance(questions, list) else []) if isinstance(q, dict))
    first = next(asked, None)
    text = None if first is None else first.get("question")
    return text if isinstance(text, str) else ""


__all__ = [
    "CHUNK",
    "DIM",
    "MAX_LINE",
    "OFF",
    "TAIL_BUDGET",
    "TAIL_NOTHING",
    "TAIL_RECORDS",
    "TOOL_INPUT_MAX",
    "YOU",
    "PendingTool",
    "TranscriptTail",
    "cells",
    "cut_cells",
    "dim_note",
    "first_question",
    "kept_input",
    "lines_backwards",
    "one_per_line",
    "parse_line",
    "question_lines",
    "record_time",
    "speaker_line",
    "wrap",
]
