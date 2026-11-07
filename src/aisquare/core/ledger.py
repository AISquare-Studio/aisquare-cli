"""The token ledger's row and the rules every agent's reader counts it by (#296's WS-0).

``services.token_ledger`` reads each session through its agent's transcript reader
(``AgentTranscript.ledger``: Claude Code's transcript, Codex's rollout), so that every
before/after number a workstream reports is counted the same way whatever agent ran it
(``docs/plans/handoff-p1-token-diet.md`` §1.7). What a request, a wake, a delta and a
compaction ARE is the same for every agent and lives here; where each sits in a
transcript is the reader's.

- **request** — one model request, counted once. Its input is ``input_fresh +
  input_cache_write + input_cache_read``, which is also the context size at that request;
  output is counted apart.
- **baseline** — the session's first request's input.
- **board read** — a shell call (:func:`is_board_read`) made of board reads
  (:data:`BOARD_READ`), a ``cd`` and filters of their output, and nothing else.
- **wake** — a Stop-hook continuation the fleet sent (:data:`WAKE_MARKER`); its window runs
  to the next turn end or prompt; a **no-action wake** is one whose window makes no tool
  call but a board read. ``wake_input`` sums the input of the requests in those windows.
- **delta** — each ``<aisquare-team-delta>`` block a hook injected (:func:`delta_blocks`),
  its characters, and its **carried** characters: each block's size times the requests
  after it, up to the next compaction, which is what every later request paid for it.
- **compaction** — the session's context compacted, by its trigger.

Read line by line, so a transcript of any size costs one line of memory; a line that does
not parse (the one the agent is writing) is skipped and counted. What comes out is
counts, never text: transcripts hold prompts.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from aisquare.core import transcripts

#: How much of a transcript's end :func:`recorded_since` looks at first.
_TAIL_BYTES = 256_000

#: What every Stop-hook wake-up the fleet sends says (``services.team._render_wakeup``).
WAKE_MARKER = "board update(s) from your fleet arrived while you were finishing this turn"

DELTA_OPEN, DELTA_CLOSE = "<aisquare-team-delta>", "</aisquare-team-delta>"
#: A delta block's opening tag, with or without attributes: since P1 WS-2 (#279) the block
#: names its board, ``<aisquare-team-delta board="…">``, and an exact match counted none.
_DELTA_OPENING = re.compile(r"<aisquare-team-delta(?:\s[^>]*)?>")

#: One command that only reads the board, as the fleet's cycles run them: ``board``,
#: ``team log|status|signals|verify|show``, ``task list|show|next`` (``next`` without
#: ``--claim``) and ``fleet ls|status``, after any global flag (``--json``). Not ``aisquare
#: log``, which is the PROMPT log, and nothing that writes.
BOARD_READ = re.compile(
    r"^(?:\S*/)?(?:aisquare|asq)(?:\s+(?:--json|--no-color|--quiet|-q|--verbose|-v))*\s+"
    r"(?:board|team\s+(?:log|status|signals|verify|show)|task\s+(?:list|show|next)"
    r"|fleet\s+(?:ls|status))(?:\s|$)"
)

#: What may stand beside a board read in one shell call: moving to a directory, and
#: filtering the read's output.
_READ_ONLY = re.compile(r"^(?:cd|head|tail|grep|jq|wc|sort|uniq|cut|tr|cat|echo|sed\s+-n)(?:\s|$)")

#: Where a shell call splits into commands: ``&&``, ``||``, ``;``, ``|``, a newline.
_COMMANDS = re.compile(r"&&|\|\||;|\||\n")

#: A redirect that writes nothing: ``2>/dev/null``, ``2>&1``.
_HARMLESS_REDIRECT = re.compile(r"\d?>\s*/dev/null|\d?>&\d")


class SessionLedger(BaseModel):
    """One session's spend, as counts (``services.token_ledger``)."""

    session_id: str
    role: str
    transcript_found: bool = False
    requests: int = 0
    input_fresh: int = 0
    input_cache_write: int = 0
    input_cache_read: int = 0
    output: int = 0
    baseline: int | None = None
    peak_context: int = 0
    mean_context: int = 0
    latest_context: int = 0
    wakes: int = 0
    no_action_wakes: int = 0
    wake_input: int = 0
    delta_blocks: int = 0
    delta_chars: int = 0
    delta_carried_chars: int = 0
    compactions: int = 0
    compaction_triggers: dict[str, int] = Field(default_factory=dict)
    tool_calls: dict[str, int] = Field(default_factory=dict)
    skipped_lines: int = 0

    @property
    def input_total(self) -> int:
        return self.input_fresh + self.input_cache_write + self.input_cache_read

    def as_counts(self) -> dict[str, Any]:
        """The row as ``--json`` writes it: every field, and ``input_total``."""
        return {**self.model_dump(mode="json"), "input_total": self.input_total}


def entries(path: Path, row: SessionLedger) -> Iterator[dict[str, Any]]:
    """Every entry of the transcript, read a line at a time; a line that does not parse is
    counted on ``row`` and skipped."""
    try:
        handle = path.open("rb")
    except OSError:
        return
    with handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                row.skipped_lines += 1
                continue
            if isinstance(entry, dict):
                yield entry


def stamped(entry: dict[str, Any]) -> datetime | None:
    """When an entry was written (its ``timestamp``), or ``None`` when it does not say."""
    raw = entry.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def recorded_since(
    path: Path,
    since: datetime,
    wanted: Callable[[dict[str, Any]], bool],
    *,
    tail_bytes: int = _TAIL_BYTES,
) -> bool:
    """Whether an entry ``wanted`` accepts was stamped at or after ``since``: the transcript's
    tail is looked at first and widened (four times over, up to the whole file) only while it
    does not yet reach back to ``since``, so asking again every few seconds for as long as a
    session lives reads its end, not its whole length."""
    size_now = transcripts.size(path)
    window = tail_bytes
    while True:
        reached = False
        for entry in transcripts.tail_entries(path, window):
            stamp = stamped(entry)
            if stamp is None:
                continue
            if stamp < since:
                reached = True
            elif wanted(entry):
                return True
        if reached or window >= size_now:
            return False
        window *= 4


def within(entry: dict[str, Any], since: datetime | None, until: datetime | None) -> bool:
    """Whether ``entry`` was written in ``[since, until]``; one with no time counts."""
    if since is None and until is None:
        return True
    stamp = stamped(entry)
    if stamp is None:
        return True
    return (since is None or stamp >= since) and (until is None or stamp <= until)


def tokens(value: Any) -> int:
    """A token count as written, ``0`` for anything that is not one."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def is_board_read(command: str) -> bool:
    """Whether a shell ``command`` only reads the board: at least one board read
    (:data:`BOARD_READ`, ``task next`` without ``--claim``), and every other command in it
    a ``cd`` or a filter of their output, with no redirect into a file."""
    if ">" in _HARMLESS_REDIRECT.sub("", command):
        return False
    parts = [part.strip() for part in _COMMANDS.split(command) if part.strip()]
    reads = [part for part in parts if BOARD_READ.match(part)]
    if not reads or any("--claim" in part for part in reads):
        return False
    return all(BOARD_READ.match(part) or _READ_ONLY.match(part) for part in parts)


def delta_blocks(text: str) -> list[str]:
    """The ``<aisquare-team-delta>`` blocks ``text`` holds, tags included."""
    found: list[str] = []
    opening = _DELTA_OPENING.search(text)
    while opening is not None:
        start = opening.start()
        end = text.find(DELTA_CLOSE, start)
        stop = len(text) if end == -1 else end + len(DELTA_CLOSE)
        found.append(text[start:stop])
        opening = _DELTA_OPENING.search(text, stop)
    return found


class Window:
    """The running state a reader keeps while it walks one transcript: the request in
    flight, the wake window, the deltas open since the last compaction, and the sums the
    row is finished with (:meth:`finish`)."""

    def __init__(self, row: SessionLedger) -> None:
        self.row = row
        self.tools: dict[str, int] = {}
        self.triggers: dict[str, int] = {}
        self.open_delta = 0
        self.in_wake = False
        self.wake_acted = False
        self.wake_requests = 0
        self.context_sum = 0

    def request(self, fresh: int, write: int, read: int, output: int) -> None:
        """One model request: its input split, its output, the context it was sent with."""
        row = self.row
        context = fresh + write + read
        row.requests += 1
        row.input_fresh += fresh
        row.input_cache_write += write
        row.input_cache_read += read
        row.output += output
        row.peak_context = max(row.peak_context, context)
        row.latest_context = context
        self.context_sum += context
        row.delta_carried_chars += self.open_delta
        if self.in_wake:
            row.wake_input += context
            self.wake_requests += 1

    def tool(self, name: str, *, board_read: bool) -> None:
        self.tools[name] = self.tools.get(name, 0) + 1
        if self.in_wake and not board_read:
            self.wake_acted = True

    def wake(self) -> None:
        """A wake begins (and closes any open one)."""
        if self.in_wake and not self.wake_acted:
            self.row.no_action_wakes += 1
        self.row.wakes += 1
        self.in_wake, self.wake_acted, self.wake_requests = True, False, 0

    def turn_ended(self) -> None:
        """The turn ended, or someone typed: a wake's window closes."""
        if self.in_wake and not self.wake_acted:
            self.row.no_action_wakes += 1
        self.in_wake = False

    def deltas(self, text: str) -> None:
        for block in delta_blocks(text):
            self.row.delta_blocks += 1
            self.row.delta_chars += len(block)
            self.open_delta += len(block)

    def compacted(self, trigger: str) -> None:
        self.row.compactions += 1
        self.triggers[trigger] = self.triggers.get(trigger, 0) + 1
        self.open_delta = 0

    def finish(self) -> SessionLedger:
        row = self.row
        if self.in_wake and not self.wake_acted:
            row.no_action_wakes += 1
        row.mean_context = round(self.context_sum / row.requests) if row.requests else 0
        row.tool_calls = dict(sorted(self.tools.items()))
        row.compaction_triggers = dict(sorted(self.triggers.items()))
        return row
