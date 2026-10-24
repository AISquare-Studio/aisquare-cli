"""The token ledger: what each agent session spent, counted from its Claude Code transcript.

One reader, so that every before/after number a workstream reports is counted the same
way and can be compared and summed (``docs/plans/handoff-p1-token-diet.md`` §1.7):

- **request** — an ``assistant`` entry with ``message.usage``, once per message id
  (Claude Code writes one entry per content block, all with the same usage); Claude
  Code's own synthetic entries are not requests. Its input is ``input_tokens +
  cache_creation_input_tokens + cache_read_input_tokens``, which is also the context
  size at that request; output is counted apart.
- **baseline** — the session's first request (``transcripts.first_turn_tokens``).
- **board read** — a shell call (:func:`is_board_read`) made of board reads
  (:data:`BOARD_READ`), a ``cd`` and filters of their output, and nothing else.
- **wake** — a Stop-hook continuation: the ``isMeta`` user entry that starts
  "Stop hook feedback:" with :data:`WAKE_MARKER`. Claude Code writes it before the
  blocked Stop's own ``stop_hook_summary``; its window runs from there to the next turn
  end (``stop_hook_summary``), prompt or wake; a **no-action wake** is one whose window
  makes no tool call but a board read. ``wake_input`` sums the
  input of the requests in those windows.
- **delta** — each ``<aisquare-team-delta>`` block a hook injected, its characters, and
  its **carried** characters: each block's size times the requests after it, up to the
  next compaction, which is what every later request paid for it.
- **compaction** — a ``compact_boundary`` entry, by its trigger.

Read line by line, so a transcript of any size costs one line of memory; a line that
does not parse (the one Claude Code is writing) is skipped and counted. What comes out
is counts, never text: transcripts hold prompts.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from aisquare.core import harness, transcripts
from aisquare.core.store import store_session
from aisquare.models import TeamSession

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
    """One session's spend, as counts (:mod:`aisquare.services.token_ledger`)."""

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


def sessions(
    project_id: str | None, *, session: str | None = None, role: str | None = None
) -> list[TeamSession]:
    """The sessions ``metrics tokens`` reads, oldest first: the project's, or every
    project's for ``None``; narrowed to an id prefix and to a role, a numbered seat
    (``coder2``) counting as its role."""
    with store_session() as store:
        projects = (
            [project.id for project in store.list_projects(all=True)]
            if project_id is None
            else [project_id]
        )
        found = [row for pid in projects for row in store.team_sessions(pid)]
    if session:
        found = [row for row in found if row.id.startswith(session)]
    if role:
        found = [row for row in found if role in (row.role, harness.base_role(row.role))]
    return sorted(found, key=lambda row: row.started_at)


def ledger(
    sessions: Iterable[TeamSession],
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[SessionLedger]:
    """One :class:`SessionLedger` per session, from the entries stamped within
    ``[since, until]`` (an entry with no timestamp counts). A session whose transcript is
    unrecorded or gone is a row of zeros, ``transcript_found`` false."""
    return [_one(session, since=since, until=until) for session in sessions]


def total(rows: Iterable[SessionLedger]) -> SessionLedger:
    """Every row summed into one, ``session_id`` ``total``: the peak is the highest, the
    mean is weighted by requests, the baseline is not summed (it is per session)."""
    summed = SessionLedger(session_id="total", role="")
    weighted = 0
    for row in rows:
        summed.transcript_found = summed.transcript_found or row.transcript_found
        for field in _SUMMED:
            setattr(summed, field, getattr(summed, field) + getattr(row, field))
        summed.peak_context = max(summed.peak_context, row.peak_context)
        weighted += row.mean_context * row.requests
        summed.compaction_triggers = dict(
            Counter(summed.compaction_triggers) + Counter(row.compaction_triggers)
        )
        summed.tool_calls = dict(Counter(summed.tool_calls) + Counter(row.tool_calls))
    summed.mean_context = round(weighted / summed.requests) if summed.requests else 0
    return summed


_SUMMED = (
    "requests",
    "input_fresh",
    "input_cache_write",
    "input_cache_read",
    "output",
    "latest_context",
    "wakes",
    "no_action_wakes",
    "wake_input",
    "delta_blocks",
    "delta_chars",
    "delta_carried_chars",
    "compactions",
    "skipped_lines",
)


def _one(session: TeamSession, *, since: datetime | None, until: datetime | None) -> SessionLedger:
    row = SessionLedger(session_id=session.id, role=session.role)
    path = Path(session.transcript_path) if session.transcript_path else None
    if path is None or not path.is_file():
        return row
    row.transcript_found = True
    row.baseline = transcripts.first_turn_tokens(path)
    tools: Counter[str] = Counter()
    triggers: Counter[str] = Counter()
    last_message: str | None = None
    open_delta = 0  # characters injected since the last compaction, carried by each request
    in_wake = False  # inside a wake's window
    wake_acted = False
    wake_requests = 0  # requests in that window so far
    context_sum = 0
    for entry in _entries(path, row):
        if not _within(entry, since, until):
            continue
        kind = entry.get("type")
        if kind == "assistant":
            tokens = transcripts.request_tokens(entry)
            message = entry.get("message")
            if tokens is None or not isinstance(message, dict):
                continue
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tools[str(block.get("name"))] += 1
                    if in_wake and not _board_read(block):
                        wake_acted = True
            mid = message.get("id")
            if mid is not None and mid == last_message:
                continue  # another content block of the same request
            last_message = mid
            usage = message["usage"]
            row.requests += 1
            row.input_fresh += _int(usage.get("input_tokens"))
            row.input_cache_write += _int(usage.get("cache_creation_input_tokens"))
            row.input_cache_read += _int(usage.get("cache_read_input_tokens"))
            row.output += _int(usage.get("output_tokens"))
            row.peak_context = max(row.peak_context, tokens)
            row.latest_context = tokens
            context_sum += tokens
            row.delta_carried_chars += open_delta
            if in_wake:
                row.wake_input += tokens
                wake_requests += 1
        elif kind == "user" and entry.get("isMeta") and WAKE_MARKER in _entry_text(entry):
            if in_wake and not wake_acted:
                row.no_action_wakes += 1
            row.wakes += 1
            in_wake, wake_acted, wake_requests = True, False, 0
        elif _ends_a_turn(entry) and not (in_wake and wake_requests == 0 and _is_summary(entry)):
            # Claude Code writes the wake first and the blocked Stop's own summary after
            # it: a summary before the window's first request closes nothing.
            if in_wake and not wake_acted:
                row.no_action_wakes += 1
            in_wake = False
        elif kind == "system" and entry.get("subtype") == "compact_boundary":
            row.compactions += 1
            meta = entry.get("compactMetadata")
            trigger = meta.get("trigger") if isinstance(meta, dict) else None
            triggers[str(trigger or "unknown")] += 1
            open_delta = 0
        elif kind == "attachment":
            for block in _deltas(entry):
                row.delta_blocks += 1
                row.delta_chars += len(block)
                open_delta += len(block)
    if in_wake and not wake_acted:
        row.no_action_wakes += 1
    row.mean_context = round(context_sum / row.requests) if row.requests else 0
    row.tool_calls = dict(sorted(tools.items()))
    row.compaction_triggers = dict(sorted(triggers.items()))
    return row


def _entries(path: Path, row: SessionLedger) -> Iterator[dict[str, Any]]:
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


def _within(entry: dict[str, Any], since: datetime | None, until: datetime | None) -> bool:
    if since is None and until is None:
        return True
    raw = entry.get("timestamp")
    if not isinstance(raw, str):
        return True
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    return (since is None or stamp >= since) and (until is None or stamp <= until)


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _entry_text(entry: dict[str, Any]) -> str:
    """A ``user`` entry's text: its string content, or its text blocks joined."""
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _is_summary(entry: dict[str, Any]) -> bool:
    return entry.get("type") == "system" and entry.get("subtype") == "stop_hook_summary"


def _ends_a_turn(entry: dict[str, Any]) -> bool:
    """Whether ``entry`` closes a wake's window: the turn ended, or someone typed."""
    if entry.get("type") == "system":
        return entry.get("subtype") == "stop_hook_summary"
    return entry.get("type") == "user" and not entry.get("isMeta") and _is_prompt(entry)


def _is_prompt(entry: dict[str, Any]) -> bool:
    """Whether a ``user`` entry is a prompt someone sent, not a tool result."""
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        kinds = {block.get("type") for block in content if isinstance(block, dict)}
        return "text" in kinds and "tool_result" not in kinds
    return False


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


def _board_read(block: dict[str, Any]) -> bool:
    """Whether a ``tool_use`` block only reads the board."""
    if block.get("name") != "Bash":
        return False
    command = (
        block.get("input", {}).get("command") if isinstance(block.get("input"), dict) else None
    )
    return isinstance(command, str) and is_board_read(command)


def _deltas(entry: dict[str, Any]) -> list[str]:
    """The ``<aisquare-team-delta>`` blocks a hook's attachment carries, tags included."""
    attachment = entry.get("attachment")
    if not isinstance(attachment, dict):
        return []
    found: list[str] = []
    for field in ("content", "stdout"):
        text = attachment.get(field)
        if not isinstance(text, str):
            continue
        opening = _DELTA_OPENING.search(text)
        while opening is not None:
            start = opening.start()
            end = text.find(DELTA_CLOSE, start)
            stop = len(text) if end == -1 else end + len(DELTA_CLOSE)
            found.append(text[start:stop])
            opening = _DELTA_OPENING.search(text, stop)
        if found:
            break  # `stdout` repeats `content` when both are set
    return found
