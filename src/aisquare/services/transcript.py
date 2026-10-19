"""The agent's conversation, rendered to terminal-ready lines (PLAN §4-M), and the tail of
it the needs-you scan classifies on.

Agent panes run their agent in tmux's ALTERNATE screen, which by design keeps no
scrollback, so `capture-pane` can never show what an agent already said (§4-L is
correct and empty for them: every live agent reports ``history_size: 0``). The
conversation itself is on disk — the JSONL transcript the board already records
as ``TeamSession.transcript_path`` — and this module turns a page of it into the
text lines the remote page writes straight into its terminal.

Each agent writes its own format, and its adapter reads it (``AgentAdapter.transcript_reader``,
``core.agent_adapters.types.AgentTranscript``): Claude Code's session JSONL, Codex's
rollout. This module chooses the reader by the row's recorded agent, opens the file, bounds
what is read of it and pages what the reader renders; the shapes and the byte and wrapping
helpers both share are in ``core.agent_adapters.transcripts``. Two properties shape
everything here:

* **The files are large.** Measured on this machine: 628 to 3 487 records, 1.4 to
  18 MB, and one pasted attachment can be megabytes on a SINGLE line. So the file
  is read BACKWARDS from its end (or from a cursor) in blocks, never whole, and
  the read stops at :data:`SCAN_BUDGET` even if the page is not full.
* **The server owns the rendering.** §4-M puts turn→text here rather than in the
  page so the two cannot disagree about what a transcript looks like, and so the
  pane stays one scroll surface: these lines go into the same terminal the live
  screen paints into.

No redaction. A transcript contains whatever the human typed or pasted,
including anything they pasted by accident. It is behind the same cookie and
passphrase as every other read and that is the whole protection.

**The tail** (:func:`read_transcript_tail`) is the other reader: not lines for a
person but the facts the needs-you scan classifies on (SPEC §4.3) — the tool
uses still waiting on an answer, what the newest record is (an interruption,
the agent's own words, a tool result), and the newest assistant text, read only as
far back as the newest model response.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aisquare.core.agent_adapters.claude_transcript import (
    INTERRUPTED_MARKER,
    REJECTED_MARKER,
    REJECTED_WITH_WORDS,
    _summarise_tool,
)
from aisquare.core.agent_adapters.transcripts import (
    MAX_LINE,
    TAIL_BUDGET,
    TAIL_RECORDS,
    TOOL_INPUT_MAX,
    PendingTool,
    TranscriptTail,
    lines_backwards,
    parse_line,
    record_time,
)
from aisquare.core.agent_adapters.transcripts import TAIL_NOTHING as _TAIL_NOTHING

if TYPE_CHECKING:
    from aisquare.core.agent_adapters.types import AgentTranscript

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


def reader_of(agent: str | None) -> AgentTranscript | None:
    """The transcript reader of the agent a row recorded (``FleetAgent.agent``: ``None`` for
    a row from before adapters, the default agent's); ``None`` for an agent this install does
    not know, or whose transcript it cannot read."""
    from aisquare.core.agent_adapters import recorded_adapter

    adapter = recorded_adapter(agent)
    return None if adapter is None else adapter.transcript_reader


@dataclass(frozen=True)
class Page:
    """One page of conversation, oldest line first (§4-M: newest last)."""

    lines: list[str]
    cursor: str | None
    """Byte offset to pass back as ``before`` for the page before this one.
    ``None`` when the beginning of the transcript is included."""
    more: bool
    stamps: dict[int, str] = field(default_factory=dict)
    """When each turn was written, as ISO UTC, by the index of the turn's first line (its
    speaker's). The one thing the page renders itself: only the phone knows its zone."""

    def page_json(self) -> dict[str, object]:
        return {
            "lines": self.lines,
            "cursor": self.cursor,
            "more": self.more,
            "stamps": {str(index): stamp for index, stamp in self.stamps.items()},
        }


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
    agent: str | None = None,
) -> Page:
    """The newest ``limit`` renderable turns ending before ``before``, as the reader of the
    row's ``agent`` renders them (:func:`reader_of`; an agent with none has an empty page).

    Reads backwards from the end (or from ``before``) in blocks, so a page costs
    the bytes it needs rather than the size of the file.
    """
    limit = max(1, min(limit, LIMIT_CAP))
    width = max(20, width)
    start = _offset(before)
    reader = reader_of(agent)
    if path is None or reader is None:
        return EMPTY
    file = Path(path)
    try:
        size = file.stat().st_size
    except OSError:
        return EMPTY
    end = size if start is None else min(start, size)
    if end <= 0:
        return EMPTY

    collected: list[tuple[list[str], str | None]] = []
    oldest = end
    reached_start = False
    try:
        for offset, raw in lines_backwards(file, end, budget=SCAN_BUDGET):
            oldest = offset
            if offset == 0:
                reached_start = True
            record = parse_line(raw)
            rendered = [] if record is None else reader.page_lines(record, width)
            if record is not None and rendered:
                collected.append((rendered, _page_stamp(record)))
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
    lines: list[str] = []
    stamps: dict[int, str] = {}
    for rendered, stamp in collected:
        if stamp is not None:
            stamps[len(lines)] = stamp
        lines.extend(rendered)
    return Page(lines=lines, cursor=cursor, more=more, stamps=stamps)


def _offset(before: str | int | None) -> int | None:
    """``before`` as a byte offset; a malformed cursor reads as "from the end"."""
    if before is None or before == "":
        return None
    try:
        value = int(before)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _page_stamp(record: dict[str, Any]) -> str | None:
    """When ``record`` was written, as ISO UTC for :attr:`Page.stamps`; ``None`` without one.

    Never a clock time: rendered here, ``astimezone()`` is the MACHINE's zone, and a fleet
    on a UTC box read from a phone in UTC-7 said ``> you 17:05`` for a prompt typed at
    10:05 by the phone's clock, on a page that tells every other time in the phone's own
    zone. Read as the tail reads it (``transcripts.record_time``), a time without a zone being
    UTC (both agents write ``Z``): this read it as the machine's local time.
    """
    when = record_time(record)
    return None if when is None else when.astimezone(UTC).isoformat(timespec="seconds")


def read_transcript_tail(
    path: Path | str | None, *, budget: int = TAIL_BUDGET, agent: str | None = None
) -> TranscriptTail | None:
    """The tail of the transcript at ``path``, as the reader of the row's ``agent`` reads it
    (:func:`reader_of`, ``AgentTranscript.tail_facts``); ``None`` when there is none to read,
    or no reader for the agent.

    The walk reads back from the end only as far as the newest model response, the human's
    own last prompt, :data:`TAIL_RECORDS` or ``budget`` bytes, whichever comes first, and a
    walk that ran out before it saw the newest response whole says so (``cut_at``): what it
    did not read may hold a tool use still waiting. Never raises: an unreadable file is
    ``None``.

    Only a regular file is opened. The path is whatever the agent's own hook
    payload said, and opening a named pipe waits for a writer: the watcher's
    scan would wait with it, every later scan behind it, and so would an action
    re-reading that agent's project. An empty file is no conversation yet,
    answered without opening it, as :func:`read_page` answers it.
    """
    reader = reader_of(agent)
    if path is None or reader is None:
        return None
    file = Path(path)
    try:
        facts = file.stat()
    except OSError:
        return None
    if not stat.S_ISREG(facts.st_mode):
        return None
    if facts.st_size == 0:
        return _TAIL_NOTHING
    written = datetime.fromtimestamp(facts.st_mtime, tz=UTC)
    try:
        return reader.tail_facts(file, facts.st_size, budget, written)
    except OSError:
        return None


#: The shared shapes and helpers, and Claude Code's markers, under the names this module
#: always had.
_lines_backwards = lines_backwards
_parse_transcript_line = parse_line
_tail_time = record_time

__all__ = [
    "DEFAULT_LIMIT",
    "EMPTY",
    "INTERRUPTED_MARKER",
    "LIMIT_CAP",
    "MAX_LINE",
    "REJECTED_MARKER",
    "REJECTED_WITH_WORDS",
    "SCAN_BUDGET",
    "TAIL_BUDGET",
    "TAIL_RECORDS",
    "TOOL_INPUT_MAX",
    "_TAIL_NOTHING",
    "Page",
    "PendingTool",
    "TranscriptTail",
    "_summarise_tool",
    "lines_backwards",
    "read_page",
    "read_transcript_tail",
    "reader_of",
]
