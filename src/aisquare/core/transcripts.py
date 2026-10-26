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
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
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

#: How far back :func:`last_reply` will read for the prompt a reply answers — a turn
#: bigger than this has lost its anchor, and says "no prompt" rather than guess.
_REPLY_MAX_BYTES = 64_000_000

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
        tokens = request_tokens(entry)
        if tokens is not None:
            return tokens
    return None


def request_tokens(entry: dict[str, Any]) -> int | None:
    """The input size of one ``assistant`` entry's request — ``input_tokens +
    cache_creation_input_tokens + cache_read_input_tokens`` — or ``None`` for an entry
    that is no request: another type, no usage, or Claude Code's own synthetic one."""
    if entry.get("type") != "assistant" or entry.get("isApiErrorMessage") is True:
        return None
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("model") == _SYNTHETIC_MODEL:
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    total = 0
    for field in _USAGE_FIELDS:
        value = usage.get(field)
        if isinstance(value, int) and not isinstance(value, bool):
            total += value
    return total


def latest_context_tokens(path: Path, *, tail_bytes: int = _TAIL_BYTES) -> int | None:
    """How large the session's context is now: the input size (:func:`request_tokens`) of
    the LAST request in the transcript. ``None`` when it has none, or cannot be read.

    Read from the tail, which grows (four times over, up to the whole file) while it holds
    no request: one turn's tool results can outweigh ``tail_bytes``.
    """
    size_now = size(path)
    window = tail_bytes
    while True:
        latest: int | None = None
        for entry in _tail_entries(path, window):
            tokens = request_tokens(entry)
            if tokens is not None:
                latest = tokens
        if latest is not None or window >= size_now:
            return latest
        window *= 4


def latest_model(path: Path, *, tail_bytes: int = _TAIL_BYTES) -> str | None:
    """The model the session's LAST reply came from (``message.model`` of its last
    ``assistant`` entry in the tail), which a ``/model`` since the launch has changed.
    ``None`` when the tail holds none, or the file cannot be read.

    One bounded read of the tail, never grown: at a Stop hook the turn's last reply is
    already on disk, followed only by the hook's own summary lines. Claude Code's synthetic
    entries (an API error in place of a reply) name no model, and a subagent's turns
    (``isSidechain``) run on a model of their own.
    """
    latest: str | None = None
    for entry in _tail_entries(path, tail_bytes):
        if entry.get("type") != "assistant" or entry.get("isSidechain") is True:
            continue
        message = entry.get("message")
        model = message.get("model") if isinstance(message, dict) else None
        if isinstance(model, str) and model and model != _SYNTHETIC_MODEL:
            latest = model
    return latest


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


def last_reply(
    path: Path,
    *,
    since: datetime | None = None,
    tail_bytes: int = _TAIL_BYTES,
    max_bytes: int = _REPLY_MAX_BYTES,
) -> str | None:
    """The assistant's text since the transcript's last prompt, or ``None`` when nothing has
    answered a prompt yet (or there is none). ``""`` is an answer without text.

    "The last prompt" is the newest ``user`` entry whose content is text the user
    sent — a tool result also arrives as a ``user`` entry, and so does what Claude
    Code writes itself (``isMeta``: a command's caveat, a hook's context); neither
    is one. What follows it is the turn that answered: every assistant text block,
    in order, joined by newlines (a tool call's own blocks are not text). For the
    captain (``services.captain.brain``): its reply to what the owner just said.

    ``since`` is when that was said: a last prompt stamped earlier is an older
    turn's, and the answer is ``None`` — the text has not reached the transcript
    yet. An entry with no ``timestamp`` cannot be told apart and counts. Nor is a
    prompt with no assistant entry after it answered: a hook that stamped the row
    ``waiting`` in the moment before the prompt's own hook said ``working`` must not
    hand back an empty reply.

    The tail read grows (four times over, up to ``max_bytes``) while it holds no prompt:
    one turn's tool results can outweigh ``tail_bytes`` — a ``read_pane`` of every
    agent — and the reply must not vanish with the prompt that anchors it.
    """
    size_now = size(path)
    window = tail_bytes
    while True:
        found = _reply_in(_tail_entries(path, window))
        if found is not None or window >= min(size_now, max_bytes):
            break
        window = min(window * 4, max_bytes)
    if found is None:
        return None
    asked_at, text = found
    if since is not None and asked_at is not None and asked_at < since:
        return None
    return text


def _reply_in(entries: Iterator[dict[str, Any]]) -> tuple[datetime | None, str | None] | None:
    """The last prompt's time and the text after it — ``None`` for the text while nothing
    has answered it — or ``None`` when no prompt is there."""
    reply: list[str] | None = None
    prompted = False
    asked_at: datetime | None = None
    for entry in entries:
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if entry.get("type") == "user" and not entry.get("isMeta") and _is_prompt(content):
            reply, prompted, asked_at = None, True, _timestamp(entry)
        elif entry.get("type") == "assistant" and prompted and isinstance(content, list):
            reply = (reply or []) + [
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
            ]
    if not prompted:
        return None
    return asked_at, "\n".join(reply).strip() if reply is not None else None


def _timestamp(entry: dict[str, Any]) -> datetime | None:
    """An entry's ``timestamp`` (``2026-09-25T10:00:00.412Z``), aware; ``None`` if unreadable."""
    raw = entry.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


def _is_prompt(content: Any) -> bool:
    """A ``user`` entry the user typed: text, not a tool result."""
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        kinds = {block.get("type") for block in content if isinstance(block, dict)}
        return "text" in kinds and "tool_result" not in kinds
    return False


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


# --- background work and activity: is a turn end a safe point (F2 of #400) ------------
#
# The background work a turn can leave running, every kind this machine's 563 transcripts
# hold (Claude Code 2.1.304 to 2.1.310; review of #423, round 2), as ``toolUseResult``:
#
# - a background Bash, ``run_in_background`` or moved there at its timeout:
#   ``{"backgroundTaskId": "b8y4zgj98", ...}``;
# - a ``Monitor`` (an agent's wait on CI or a condition): ``{"taskId": "brlrwkgkh",
#   "timeoutMs": 1800000, "persistent": false}``;
# - a background subagent: ``{"isAsync": true, "status": "async_launched", "agentId": …}``;
# - a background forked skill: ``{"status": "forked", "background": true, "agentId": …}``.
#
# Each reports in a ``<task-notification>`` naming it — queued, then taken — and its FINAL one
# carries a ``<status>`` (completed, failed, killed, stopped). A Monitor first sends events
# with no ``<status>``, one or many, so only a notice with a status ends a task. ``TaskStop``
# ends one too, its call and its result (``{"task_id": …}``), and so does a Monitor's own
# ``timeoutMs`` (unless ``persistent``). A shape with an id this list does not know is counted
# as running until it reports, fails closed; the two foreground shapes that carry an id are
# not: a subagent that answered inline (``"status": "completed"``) and a skill forked in the
# foreground (``"status": "forked"`` without ``"background": true``).

_LAUNCH_MARKS = (
    b'"backgroundTaskId"',
    b'"taskId"',
    b'"agentId"',
    b'"shellId"',
    b"<task-id>",
    b'"TaskStop"',
    b'"task_id"',
)
"""What a line must hold to say anything of background work: a launch, a notice, a stop. Every
other line is skipped unparsed, so a scan of an 18 MB transcript parses a handful of them."""
_NOTICE = re.compile(rb"<task-notification>(.*?)(?=<task-notification>|</task-notification>|$)")
_NOTICE_ID = re.compile(rb"<task-id>\s*([^<\s]+)\s*</task-id>")
_NOTICE_STATUS = re.compile(rb"<status>\s*([^<\s]+)\s*</status>")
_RAW_IDS = re.compile(rb'"(?:backgroundTaskId|taskId|agentId|shellId)"\s*:\s*"([^"]+)"')
_FOREGROUND = frozenset({"completed", "failed", "error", "cancelled", "canceled", "interrupted"})
"""A subagent result's ``status`` when it answered inline: it runs no more."""
_LAUNCH_ID_KEYS = ("backgroundTaskId", "taskId", "agentId", "shellId")


def background_running(
    path: Path, *, since: datetime | None = None, now: datetime | None = None
) -> list[str]:
    """The ids of the background work the transcript at ``path`` started and has not seen end
    (the kinds above): a Bash in the background, a ``Monitor``, a background subagent or forked
    skill, or a shape not known that carries an id — with no notice with a ``<status>`` naming
    it after the launch, no ``TaskStop`` of it, and for a Monitor, its ``timeoutMs`` not past
    at ``now``.

    A turn can end with such work still running, and the agent's own process runs it: an
    ``/exit`` then kills it, and a resumed conversation waits for a notice that never comes.
    ``[accounts] before_limit`` moves nobody while it runs (``services.hooks``).

    Work a SUBAGENT launched is the session's too: its launch is in the subagent's own
    transcript, ``<session>/subagents/agent-<id>.jsonl`` beside ``path``, and its notice lands
    in the main one (review of #423, round 3: 19 on this machine, 6 of 10 traced still running
    at a main turn end). Those files are read for launches and stops first, then ``path``,
    whose notices end them.

    ``since`` leaves out launches written before it — the start of the process that runs the
    agent now (its fleet row's): a restart or a switch resumes the conversation, not the work,
    and its launches would otherwise read as running for good. A launch on a line too long to
    parse (:data:`_LINE_MAX_BYTES`) is read by its id alone and counted. Read whole, line by
    line; ``OSError`` when a file cannot be read.
    """
    running: dict[str, datetime | None] = {}
    for transcript in (*_subagent_transcripts(path), path):
        _read_background(transcript, running, since)
    moment = now or datetime.now(tz=UTC)
    return [task for task, until in running.items() if until is None or until > moment]


def _subagent_transcripts(path: Path) -> list[Path]:
    """The transcripts of the subagents the session at ``path`` ran: ``<session>/subagents``."""
    folder = path.with_suffix("") / "subagents"
    return sorted(folder.glob("*.jsonl")) if folder.is_dir() else []


def _read_background(
    path: Path, running: dict[str, datetime | None], since: datetime | None
) -> None:
    """Add the launches of ``path`` to ``running`` and take out what ended there."""
    with path.open("rb") as handle:
        for raw in handle:
            if not any(mark in raw for mark in _LAUNCH_MARKS):
                continue
            for task in _notices_with_a_status(raw):
                running.pop(task, None)
            record = _parsed(raw)
            if record is None:
                if len(raw) > _LINE_MAX_BYTES:
                    for match in _RAW_IDS.findall(raw):
                        running[match.decode(errors="replace")] = None
                continue
            for stopped in _stopped_ids(record):
                running.pop(stopped, None)
            when = _timestamp(record)
            if since is not None and when is not None and when < since:
                continue
            for task, until in _launches(record, when):
                running[task] = until


def _notices_with_a_status(raw: bytes) -> list[str]:
    """The ids of the ``<task-notification>`` blocks in ``raw`` that carry a ``<status>``: a
    task's final notice. A Monitor's events carry none, and end nothing."""
    ended: list[str] = []
    for block in _NOTICE.findall(raw):
        found = _NOTICE_ID.search(block)
        if found is not None and _NOTICE_STATUS.search(block) is not None:
            ended.append(found.group(1).decode(errors="replace"))
    return ended


def _launches(record: dict[str, Any], when: datetime | None) -> list[tuple[str, datetime | None]]:
    """The background work ``record``'s tool result started, each with when it ends of itself
    (a Monitor's timeout), else ``None``: until it reports."""
    result = record.get("toolUseResult")
    if not isinstance(result, dict):
        return []
    task = result.get("taskId")
    if isinstance(task, str) and task:
        timeout = result.get("timeoutMs")
        until = None
        if (
            result.get("persistent") is not True
            and isinstance(timeout, int | float)
            and timeout > 0
            and when is not None
        ):
            until = when + timedelta(milliseconds=timeout)
        return [(task, until)]
    status = result.get("status")
    agent = result.get("agentId")
    if isinstance(agent, str) and agent:
        inline = status in _FOREGROUND or (
            status == "forked" and result.get("background") is not True
        )
        return [] if inline else [(agent, None)]
    return [
        (value, None)
        for key in _LAUNCH_ID_KEYS
        if isinstance(value := result.get(key), str) and value
    ]


def _stopped_ids(record: dict[str, Any]) -> list[str]:
    """The ids ``record``'s ``TaskStop`` names: the agent's call (``input.task_id``, or an older
    ``shell_id``) or its result (``toolUseResult.task_id``)."""
    found: list[str] = []
    result = record.get("toolUseResult")
    if isinstance(result, dict) and not any(key in result for key in _LAUNCH_ID_KEYS):
        stopped = result.get("task_id")
        if isinstance(stopped, str) and stopped:
            found.append(stopped)
    message = record.get("message")
    if not isinstance(message, dict):
        return found
    for block in _content_blocks(message.get("content")):
        tool_input = block.get("input")
        if block.get("type") != "tool_use" or block.get("name") != "TaskStop":
            continue
        if isinstance(tool_input, dict):
            found.extend(
                value
                for key in ("task_id", "shell_id")
                if isinstance(value := tool_input.get(key), str) and value
            )
    return found


_ACTIVITY_MARKS = (
    b'"type":"user"',
    b'"type":"assistant"',
    b'"queue-operation"',
    b'"stop_hook_summary"',
    b'"type": "user"',
    b'"type": "assistant"',
)


def activity_since(path: Path, offset: int) -> bool:
    """Whether the transcript at ``path`` shows a turn begun after byte ``offset`` — a turn
    end's size — or one about to begin.

    Begun: a ``user`` record (a prompt, a task's notice, a tool's result), an ``assistant`` one,
    or a queued message taken (``queue-operation`` ``dequeue``) after ``offset``. A prompt's own
    record is written once its ``UserPromptSubmit`` hook has passed (a prompt that hook blocks
    gets none), so this sees a typed prompt no sooner than its board stamp does; it is a
    queued message or a task's notice, whose ``enqueue`` and ``dequeue`` are on disk at once,
    that it sees first. What Claude Code writes at an idle prompt (``system`` records, titles,
    modes, attachments) is no turn.

    About to begin: a message queued during the turn and not yet taken — an ``enqueue`` since
    the last turn end the queue lived through (a ``stop_hook_summary``, written after the Stop
    hooks have run) with no ``dequeue`` or ``remove`` after it — which Claude Code submits as
    the turn ends. Counted from the last turn end only: over the whole file, one enqueue never
    matched (the same notice queued twice, taken once) read as busy at every turn end after
    it, and the agent was never moved (review of #423, round 2). A file shorter than
    ``offset`` (rewritten) counts as activity. ``OSError`` when it cannot be read.
    """
    queued = 0
    with path.open("rb") as handle:
        if handle.seek(0, 2) < offset:
            return True
        handle.seek(0)
        position = 0
        for raw in handle:
            start, position = position, position + len(raw)
            if not any(mark in raw for mark in _ACTIVITY_MARKS):
                continue
            record = _parsed(raw)
            if record is None:
                if start >= offset and len(raw) > _LINE_MAX_BYTES:
                    return True  # too long to read: a pasted prompt as like as anything
                continue
            kind = record.get("type")
            if kind == "system" and record.get("subtype") == "stop_hook_summary":
                queued = 0
            elif kind == "queue-operation":
                operation = record.get("operation")
                if operation == "enqueue":
                    queued += 1
                elif operation in ("dequeue", "remove"):
                    queued = max(0, queued - 1)
                    if operation == "dequeue" and start >= offset:
                        return True
            elif start >= offset and kind in ("user", "assistant"):
                return True
    return queued > 0


_LINE_MAX_BYTES = 262_144
"""Longest line the background and activity scans parse (a pasted attachment can be megabytes
on one line); a longer one is read by its ids alone, or counted as activity."""


def _parsed(raw: bytes) -> dict[str, Any] | None:
    if len(raw) > _LINE_MAX_BYTES:
        return None
    try:
        record = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return record if isinstance(record, dict) else None


def _content_blocks(content: object) -> list[dict[str, Any]]:
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []
