"""Claude Code's answer to ``AgentTranscript.ledger``, ``context_tokens`` and
``compacted_since``: main's token ledger reader (#296's WS-0), moved behind the adapter.

Where :mod:`aisquare.core.ledger`'s terms sit in a Claude Code transcript:

- **request** — an ``assistant`` entry with ``message.usage``, once per message id (Claude
  Code writes one entry per content block, all with the same usage); its own synthetic
  entries are not requests (``core.transcripts.request_tokens``). Input is
  ``input_tokens`` (fresh), ``cache_creation_input_tokens`` (written) and
  ``cache_read_input_tokens`` (read).
- **baseline** — ``core.transcripts.first_turn_tokens``.
- **tool call** — a ``tool_use`` block, by name; a board read is a ``Bash`` one.
- **wake** — the ``isMeta`` user entry that starts "Stop hook feedback:" with the marker.
  Claude Code writes it before the blocked Stop's own ``stop_hook_summary``; its window runs
  from there to the next turn end (``stop_hook_summary``), prompt or wake.
- **delta** — the blocks a hook's ``attachment`` carries (``content``, else ``stdout``).
- **compaction** — a ``compact_boundary`` system entry, by ``compactMetadata.trigger``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from aisquare.core import ledger, transcripts
from aisquare.core.ledger import SessionLedger


def read(file: Path, *, since: datetime | None, until: datetime | None) -> SessionLedger:
    """The counts of the transcript ``file``, from the entries stamped in ``[since, until]``."""
    row = SessionLedger(session_id="", role="", transcript_found=True)
    row.baseline = transcripts.first_turn_tokens(file)
    window = ledger.Window(row)
    last_message: str | None = None
    for entry in ledger.entries(file, row):
        if not ledger.within(entry, since, until):
            continue
        kind = entry.get("type")
        if kind == "assistant":
            size = transcripts.request_tokens(entry)
            message = entry.get("message")
            if size is None or not isinstance(message, dict):
                continue
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    window.tool(str(block.get("name")), board_read=_board_read(block))
            mid = message.get("id")
            if mid is not None and mid == last_message:
                continue  # another content block of the same request
            last_message = mid
            usage = message["usage"]
            window.request(
                ledger.tokens(usage.get("input_tokens")),
                ledger.tokens(usage.get("cache_creation_input_tokens")),
                ledger.tokens(usage.get("cache_read_input_tokens")),
                ledger.tokens(usage.get("output_tokens")),
            )
        elif kind == "user" and entry.get("isMeta") and ledger.WAKE_MARKER in _entry_text(entry):
            window.wake()
        elif _ends_a_turn(entry) and not (
            window.in_wake and window.wake_requests == 0 and _is_summary(entry)
        ):
            # Claude Code writes the wake first and the blocked Stop's own summary after
            # it: a summary before the window's first request closes nothing.
            window.turn_ended()
        elif kind == "system" and entry.get("subtype") == "compact_boundary":
            meta = entry.get("compactMetadata")
            trigger = meta.get("trigger") if isinstance(meta, dict) else None
            window.compacted(str(trigger or "unknown"))
        elif kind == "attachment":
            for text in _attachment_texts(entry):
                window.deltas(text)
    return window.finish()


def context_tokens(file: Path) -> int | None:
    """The context now: the last request's input (``core.transcripts``)."""
    return transcripts.latest_context_tokens(file)


def compacted_since(file: Path, since: datetime) -> bool:
    """Whether a ``compact_boundary`` was written at or after ``since`` (the transcript's
    tail is read: ``ledger.recorded_since``)."""
    return ledger.recorded_since(
        file,
        since,
        lambda entry: entry.get("type") == "system" and entry.get("subtype") == "compact_boundary",
    )


def delta_blocks(entry: dict[str, Any]) -> list[str]:
    """The ``<aisquare-team-delta>`` blocks a hook's ``attachment`` entry carries, tags
    included."""
    return [block for text in _attachment_texts(entry) for block in ledger.delta_blocks(text)]


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


def _board_read(block: dict[str, Any]) -> bool:
    """Whether a ``tool_use`` block only reads the board."""
    if block.get("name") != "Bash":
        return False
    command = (
        block.get("input", {}).get("command") if isinstance(block.get("input"), dict) else None
    )
    return isinstance(command, str) and ledger.is_board_read(command)


def _attachment_texts(entry: dict[str, Any]) -> list[str]:
    """The texts a hook's attachment carries that may hold deltas: ``content``, else
    ``stdout`` (which repeats it when both are set)."""
    attachment = entry.get("attachment")
    if not isinstance(attachment, dict):
        return []
    for field in ("content", "stdout"):
        text = attachment.get(field)
        if isinstance(text, str) and ledger.delta_blocks(text):
            return [text]
    return []
