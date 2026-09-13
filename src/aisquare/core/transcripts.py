"""Reading a Claude Code transcript (JSONL) for two facts, cheaply.

Both readers are bounded — the head of the file for the first turn, the tail
for the latest tool results — because a transcript can run to hundreds of
megabytes and both callers run on hot paths (a hook, a doctor line, a spawn).
Neither raises for a file that is missing, truncated or mid-write: a line that
is not JSON is skipped, and an answer that cannot be read is ``None`` / ``0``.

What a transcript looks like (Claude Code 2.1.x, one JSON object per line)::

    {"type": "assistant", "message": {"model": "claude-opus-5", "usage": {
        "input_tokens": 2, "cache_creation_input_tokens": 7596,
        "cache_read_input_tokens": 130041, "output_tokens": 302, ...}, ...}}
    {"type": "user", "message": {"content": [{"type": "tool_result",
        "content": "claude-opus-5[1m] is temporarily unavailable (server error), so
                    auto mode cannot determine the safety of Bash right now. ...",
        "is_error": true}]}, "toolUseResult": "Error: ..."}

(shapes read off this machine's transcripts while working #150).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

#: The sentence Claude Code puts in the tool result when auto mode's CLASSIFIER
#: request failed — not the chat model. From the permission-modes reference:
#: "A separate message that names a model and says auto mode 'cannot determine
#: the safety' of an action means a classifier request failed." (#150)
REFUSAL_MARKER = "auto mode cannot determine the safety of"

#: How much of a transcript's head the first-turn reader is willing to scan.
#: The first assistant turn follows the session's opening entries (a handful
#: of KB); a file with no assistant turn in its first megabyte has none yet.
_HEAD_BYTES = 1_000_000

#: How much of a transcript's tail the refusal counter reads: enough for a
#: turn's worth of tool results, small enough to cost a hook nothing.
_TAIL_BYTES = 256_000

_USAGE_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def first_turn_tokens(path: Path) -> int | None:
    """The size of the session's FIRST assistant request, in input tokens.

    ``input_tokens + cache_creation_input_tokens + cache_read_input_tokens`` of
    the first ``assistant`` entry that carries ``message.usage`` — the whole
    prompt the model was handed before the operator had typed anything much:
    system prompt, tool schemas (every MCP connector's included), skills,
    memory. That is the session's BASELINE, and what a classifier call is at
    least as large as (#150). ``None`` when the file has no such entry within
    the head of the file, or cannot be read.
    """
    for entry in _head_entries(path):
        if entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(usage, dict):
            continue
        total = 0
        for field in _USAGE_FIELDS:
            value = usage.get(field)
            if isinstance(value, int) and not isinstance(value, bool):
                total += value
        return total
    return None


def refusal_count(path: Path, *, tail_bytes: int = _TAIL_BYTES) -> int:
    """How many tool results in the transcript's TAIL are auto-mode refusals.

    Counts ``tool_result`` blocks whose text carries :data:`REFUSAL_MARKER` —
    what Claude Code hands the model back when the classifier request failed.
    Only tool results count: an operator pasting the sentence into a prompt, or
    an assistant quoting a doc that contains it, is neither a refusal nor
    evidence of one. ``0`` for a file that is missing or unreadable.
    """
    count = 0
    for entry in _tail_entries(path, tail_bytes):
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            if REFUSAL_MARKER in _block_text(block):
                count += 1
    return count


def _block_text(block: dict[str, Any]) -> str:
    """A tool result's text, whether it is a string or a list of text parts."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _head_entries(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open("rb") as handle:
            raw = handle.read(_HEAD_BYTES)
    except OSError:
        return
    yield from _entries(raw.split(b"\n"))


def _tail_entries(path: Path, tail_bytes: int) -> Iterator[dict[str, Any]]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(size - tail_bytes, 0))
            raw = handle.read()
    except OSError:
        return
    lines = raw.split(b"\n")
    if len(raw) >= tail_bytes:
        lines = lines[1:]  # the first line is almost surely a partial one
    yield from _entries(lines)


def _entries(lines: list[bytes]) -> Iterator[dict[str, Any]]:
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(entry, dict):
            yield entry
