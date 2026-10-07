"""The agent's conversation, rendered to terminal-ready lines (PLAN §4-M).

Agent panes run Claude Code in tmux's ALTERNATE screen, which by design keeps no
scrollback, so `capture-pane` can never show what an agent already said (§4-L is
correct and empty for them: every live agent reports ``history_size: 0``). The
conversation itself is on disk — the JSONL transcript the board already records
as ``TeamSession.transcript_path`` — and this module turns a page of it into the
text lines the remote page writes straight into its terminal.

Two properties shape everything here:

* **The files are large.** Measured on this machine: 628 to 3 487 records, 1.4 to
  18 MB, and one pasted attachment can be megabytes on a SINGLE line. So the file
  is read BACKWARDS from its end (or from a cursor) in blocks, never whole, and
  the read stops at :data:`SCAN_BUDGET` even if the page is not full.
* **The server owns the rendering.** §4-M puts turn→text here rather than in the
  page so the two cannot disagree about what a transcript looks like, and so the
  pane stays one scroll surface: these lines go into the same terminal the live
  screen paints into.

What is rendered, and what is deliberately not: a user's own words and an
assistant's text are the conversation and are shown; a tool call is one dim
summary line, because a transcript without them reads as a monologue with
inexplicable gaps; tool RESULTS and thinking blocks are skipped. Results are the
bulk of those megabytes and are the agent's input rather than its response, and
the ask was to see responses. A "user" record carrying only tool results is
plumbing, not a person, and is never labelled as one.

No redaction. A transcript contains whatever the human typed or pasted,
including anything they pasted by accident. It is behind the same cookie and
passphrase as every other read and that is the whole protection.
"""

from __future__ import annotations

import json
import textwrap
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_LIMIT = 200
"""Turns per page when the caller does not say (PLAN §4-M)."""

LIMIT_CAP = 1000
"""Most turns one request may return, however many it asks for."""

SCAN_BUDGET = 2_000_000
"""Most bytes one request will read off the end of a transcript.

A page of 200 turns is a few hundred KB of an ordinary transcript. This stops a
file whose tail is one enormous pasted attachment from turning a page request
into an 18 MB read; the page comes back short with ``more`` true instead.
"""

MAX_LINE = 262_144
"""Longest JSONL line that is parsed at all.

Attachments and tool results can be megabytes on one line. They render to
nothing anyway, so they are skipped WITHOUT being handed to ``json.loads`` —
otherwise the cost of ignoring a 10 MB paste would be parsing it first.
"""

_CHUNK = 65_536

_DIM = "\x1b[2m"
_OFF = "\x1b[0m"
_SPEAKERS = {
    "user": "\x1b[1;36m> you\x1b[0m",
    "assistant": "\x1b[1;32m* claude\x1b[0m",
}
"""Who is talking, as the terminal shows it. Plain ASCII markers on purpose: the
lines land in someone's terminal font, and a glyph that renders as a box on one
phone is worse than a character everything has."""


@dataclass(frozen=True)
class Page:
    """One page of conversation, oldest line first (§4-M: newest last)."""

    lines: list[str]
    cursor: str | None
    """Byte offset to pass back as ``before`` for the page before this one.
    ``None`` when the beginning of the transcript is included."""
    more: bool

    def page_json(self) -> dict[str, object]:
        return {"lines": self.lines, "cursor": self.cursor, "more": self.more}


EMPTY = Page(lines=[], cursor=None, more=False)
"""What a missing, empty or unreadable transcript returns — never an error.

An agent whose transcript has not been written yet, or was cleaned up, must
still open in the page (§4-M).
"""


def read_page(
    path: Path | str | None,
    *,
    limit: int = DEFAULT_LIMIT,
    before: str | int | None = None,
    width: int = 80,
) -> Page:
    """The newest ``limit`` renderable turns ending before ``before``.

    Reads backwards from the end (or from ``before``) in blocks, so a page costs
    the bytes it needs rather than the size of the file.
    """
    limit = max(1, min(limit, LIMIT_CAP))
    width = max(20, width)
    start = _offset(before)
    if path is None:
        return EMPTY
    file = Path(path)
    try:
        size = file.stat().st_size
    except OSError:
        return EMPTY
    end = size if start is None else min(start, size)
    if end <= 0:
        return EMPTY

    collected: list[tuple[int, list[str]]] = []
    oldest = end
    reached_start = False
    try:
        for offset, raw in _lines_backwards(file, end):
            oldest = offset
            if offset == 0:
                reached_start = True
            rendered = _render_transcript_record(_parse_transcript_line(raw), width)
            if rendered:
                collected.append((offset, rendered))
                if len(collected) >= limit:
                    break
        else:
            # The generator ran out: either the file start, or the scan budget.
            reached_start = reached_start or end <= SCAN_BUDGET
    except OSError:
        return EMPTY

    # "Is there more" is whether the READ reached the start of the file, not
    # whether the first RENDERED turn sits at offset 0 — a transcript opens with
    # metadata records (custom-title, mode, agent-name), so the oldest thing a
    # person said is never at byte 0 and keying on that reported more=True
    # forever on a fully-served file.
    more = not reached_start
    # The cursor is the oldest line this read EXAMINED, rendered or not. Keyed on
    # the oldest rendered turn, a budget spent inside a stretch that renders
    # nothing (a pasted image longer than the budget) handed back the cursor it
    # was given, and the next page re-read the same bytes, found nothing again,
    # and answered `more` with no cursor at all: the start of the conversation
    # could never be reached. The reader yields a line cut by the budget once it
    # is too long to parse anyway, so this always moves (``_lines_backwards``).
    cursor = str(oldest) if more else None
    if not collected:
        return Page(lines=[], cursor=cursor, more=more)
    collected.reverse()
    lines = [line for _offset, rendered in collected for line in rendered]
    return Page(lines=lines, cursor=cursor, more=more)


def _offset(before: str | int | None) -> int | None:
    """``before`` as a byte offset; a malformed cursor reads as "from the end"."""
    if before is None or before == "":
        return None
    try:
        value = int(before)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _lines_backwards(
    file: Path, end: int, *, budget: int = SCAN_BUDGET
) -> Iterator[tuple[int, bytes]]:
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
            size = min(_CHUNK, position)
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


def _parse_transcript_line(raw: bytes) -> dict[str, Any] | None:
    """One JSONL record, or ``None`` for anything that cannot be one usefully."""
    if len(raw) > MAX_LINE:
        return None
    try:
        record = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return record if isinstance(record, dict) else None


def _blocks(content: object) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def _stamp(record: dict[str, Any]) -> str:
    raw = record.get("timestamp")
    if not isinstance(raw, str):
        return ""
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return when.astimezone().strftime("%H:%M")


def _summarise_tool(block: dict[str, Any]) -> str:
    name = block.get("name")
    name = name if isinstance(name, str) and name else "tool"
    payload = block.get("input")
    detail = ""
    if isinstance(payload, dict):
        for key in ("command", "file_path", "path", "pattern", "query", "prompt", "url"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                detail = value.strip().splitlines()[0]
                break
    return f"{name}({detail[:72]})" if detail else name


def _wrap(text: str, width: int, *, indent: str = "  ") -> list[str]:
    out: list[str] = []
    for paragraph in text.replace("\r\n", "\n").split("\n"):
        stripped = paragraph.rstrip()
        if not stripped:
            out.append("")
            continue
        out.extend(
            textwrap.wrap(
                stripped,
                width=width,
                initial_indent=indent,
                subsequent_indent=indent,
                replace_whitespace=False,
                drop_whitespace=True,
                break_long_words=True,
                break_on_hyphens=False,
            )
            or [indent]
        )
    return out


def _render_transcript_record(record: dict[str, Any] | None, width: int) -> list[str]:
    """One transcript record as terminal lines, or ``[]`` when it is not conversation."""
    if record is None or record.get("type") not in ("user", "assistant"):
        return []
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    role = message.get("role") or record.get("type")
    blocks = _blocks(message.get("content"))
    if not blocks:
        return []

    body: list[str] = []
    for block in blocks:
        kind = block.get("type")
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                body.extend(_wrap(text, width))
        elif kind == "tool_use":
            body.append(f"{_DIM}  ⎿ {_summarise_tool(block)}{_OFF}")
        # thinking and tool_result are deliberately not rendered — see the module
        # docstring. A user record carrying only tool_result therefore has no
        # body, and falls out below rather than being labelled as the person.
    if not body:
        return []

    stamp = _stamp(record)
    when = f"{_DIM} {stamp}{_OFF}" if stamp else ""
    speaker = _SPEAKERS["user" if role == "user" else "assistant"]
    return [f"{speaker}{when}", *body, ""]


__all__ = [
    "DEFAULT_LIMIT",
    "EMPTY",
    "LIMIT_CAP",
    "MAX_LINE",
    "SCAN_BUDGET",
    "Page",
    "read_page",
]
