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

(shapes read off this machine's transcripts while working #150). One more
assistant shape is not a turn at all: when a request fails, Claude Code writes
an entry of its own in place of the reply — ``"model": "<synthetic>"``,
``"isApiErrorMessage": true`` — and every usage field on it is zero.
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

#: The longest tool-result text that can still be a refusal. Claude Code's own
#: sentence is ~170 characters, but the tool result need not stop there: the
#: auto-mode denials it writes carry a guidance paragraph after their sentence
#: and run to ~1,050, and #150 quotes this one as going on past "try this
#: action again". A cap under the real length zeroes every count — no bell, no
#: doctor evidence — while tests on the bare sentence stay green, so it sits
#: about four times above that. What it still turns away is a tool's long
#: OUTPUT that happens to open with the sentence.
_REFUSAL_MAX_CHARS = 4_000

#: The model name Claude Code gives an assistant entry it writes itself — an
#: API error in place of a reply — rather than one the API returned.
_SYNTHETIC_MODEL = "<synthetic>"

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

    Claude Code's SYNTHETIC entries are skipped: when a request fails it writes
    an assistant entry of its own (``model: "<synthetic>"``,
    ``isApiErrorMessage``) whose usage is all zeros. Taken as the first turn,
    a session whose first request failed would measure a baseline of 0 — an
    ``ok`` doctor line and a silent spawn for exactly the machine being refused.
    """
    for entry in _head_entries(path):
        if entry.get("type") != "assistant" or entry.get("isApiErrorMessage") is True:
            continue
        message = entry.get("message")
        if not isinstance(message, dict) or message.get("model") == _SYNTHETIC_MODEL:
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        total = 0
        for field in _USAGE_FIELDS:
            value = usage.get(field)
            if isinstance(value, int) and not isinstance(value, bool):
                total += value
        return total
    return None


def refusal_count(path: Path, *, tail_bytes: int = _TAIL_BYTES, since: int = 0) -> int:
    """How many tool results in the transcript's TAIL are auto-mode refusals.

    Counts the ``tool_result`` blocks that ARE the refusal — what Claude Code
    hands the model back when the classifier request failed: flagged
    ``is_error``, short, with :data:`REFUSAL_MARKER` in its first line. Only
    that shape counts: an operator pasting the sentence into a prompt, an
    assistant quoting a doc, and a tool's output that merely CONTAINS it — a
    Read of this module or of the docs that name the signature, a grep, a test
    run — are none of them a refusal, and an agent working on this repository
    reads all three. ``0`` for a file that is missing or unreadable.

    ``since`` is a byte offset — :func:`size` at an earlier moment — before
    which nothing is counted, however far the tail reaches back: ``claude
    --resume`` appends to the same file, so a replacement that resumes a
    transcript would otherwise be counted the refusals of the launch it
    replaced (#150).
    """
    count = 0
    for entry in _tail_entries(path, tail_bytes, since):
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if _is_refusal(block):
                count += 1
    return count


def size(path: Path) -> int:
    """How long the transcript is now, in bytes — an offset :func:`refusal_count` can
    start at later. ``0`` for a file that is missing or unreadable."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _is_refusal(block: Any) -> bool:
    """Whether one content block is Claude Code's refusal itself, not a quote of it."""
    if not isinstance(block, dict) or block.get("type") != "tool_result":
        return False
    if block.get("is_error") is not True:
        return False
    text = _block_text(block).strip()
    if len(text) > _REFUSAL_MAX_CHARS:
        return False
    first_line = text.split("\n", 1)[0]
    return REFUSAL_MARKER in first_line


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


def _tail_entries(path: Path, tail_bytes: int, since: int = 0) -> Iterator[dict[str, Any]]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            end = handle.tell()
            start = max(end - tail_bytes, since, 0)
            handle.seek(start)
            raw = handle.read()
    except OSError:
        return
    lines = raw.split(b"\n")
    if start > since:
        # The read began at an arbitrary byte, not at the start of the file or at
        # an offset `size` gave: the first line is almost surely a partial one.
        lines = lines[1:]
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
