"""Claude Code's transcript, read for Remote: its session JSONL, as the board records it
(``TeamSession.transcript_path``). :class:`ClaudeTranscript` is Claude Code's answer to the
transcript slot (``AgentAdapter.transcript_reader``, ``types.AgentTranscript``).

What is rendered, and what is deliberately not: a user's own words and an
assistant's text are the conversation and are shown; a tool call is one dim
summary line, because a transcript without them reads as a monologue with
inexplicable gaps; tool RESULTS and thinking blocks are skipped. Results are the
bulk of those megabytes and are the agent's input rather than its response, and
the ask was to see responses. A "user" record carrying only tool results is
plumbing, not a person, and is never labelled as one.

**The tail** (:func:`_tail_walk`) is the other reader: not lines for a person but the
facts the needs-you scan classifies on (SPEC §4.3) — the tool uses still waiting on an
answer, what the newest record is (an interruption, the agent's own words, a tool
result), and the newest assistant text. Claude Code starts no assistant message until
every tool result of the one before is in, so only the newest message can hold a pending
tool use, and the walk stops at the message before it: a working agent costs its newest
message, not its whole history.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aisquare.core.agent_adapters.transcripts import (
    DIM,
    MAX_LINE,
    OFF,
    TAIL_RECORDS,
    YOU,
    PendingTool,
    TranscriptTail,
    dim_note,
    first_question,
    kept_input,
    lines_backwards,
    one_per_line,
    parse_line,
    question_lines,
    record_time,
    speaker_line,
    wrap,
)
from aisquare.core.agent_adapters.types import AnswerKey, AnswerKind, CallAsk, UsageScan
from aisquare.core.ledger import SessionLedger

SPEAKER = "claude"

_TOOL_USE_ID = re.compile(rb'"tool_use_id"\s*:\s*"([^"]+)"')
"""A tool result's pairing key, found in a line too long to parse (an escaped ``\\"`` inside a
string never matches, so a pasted transcript cannot answer a tool use it quotes)."""

_TOOL_USE_BLOCK = re.compile(rb'"type"\s*:\s*"tool_use"')
"""A tool use in a line too long to parse, found the same way, as a key and not as text."""

_TOOL_USE_BLOCK_ID = re.compile(
    rb'"id"\s*:\s*"(toolu_[^"\\]+)"(?:\s*,\s*"name"\s*:\s*"([^"\\]{1,64})")?'
)
"""Such a tool use's id (the API's ``toolu_`` form), and its name where it follows the id, as
the API writes the block."""

_UNPARSED_ASIDE = re.compile(rb'"is(?:Sidechain|Meta)"\s*:\s*true')
"""A line too long to parse that is a sub-agent's or an injected record, which the tail skips."""

INTERRUPTED_MARKER = "[Request interrupted by user"
"""How Claude Code 2.1.292 records an Esc, as the text of a user record (a prefix: the
rejection of a tool use adds `` for tool use]``). A Claude Code string, not a contract."""

REJECTED_MARKER = "The user doesn't want to proceed"
"""How the error result of a tool use the human rejected begins, in Claude Code 2.1.292."""

REJECTED_WITH_WORDS = "To tell you how to proceed, the user said:"
"""What that result says, in place of "STOP what you are doing", when the human turned the
tool use down with words for the agent ("No, and tell Claude what to do differently").
Claude Code does not stop the turn then: the agent goes on, to work on them."""

_ASKS: Mapping[str, CallAsk] = {
    "AskUserQuestion": "question",
    "ExitPlanMode": "plan",
    "Task": "subagent",
    "Agent": "subagent",
}
"""What a call of these tools waits on the human for: an ``AskUserQuestion`` or an
``ExitPlanMode`` IS its dialog; a sub-agent runs inside a ``Task``/``Agent``, whose own
tool uses are in its own records."""

_FILE_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})
"""The tools whose prompt names the file by its base name, in the line above its options
("Do you want to make this edit to cache.py?"), however tall its diff."""

_ANSWERS: Mapping[AnswerKind, tuple[AnswerKey, ...]] = {
    "permission": (AnswerKey("1", ("1",)), AnswerKey("2", ("2",)), AnswerKey("No", ("Escape",))),
    "plan": (
        AnswerKey("1", ("1",)),
        AnswerKey("2", ("2",)),
        AnswerKey("3", ("3",)),
        AnswerKey("Keep planning", ("Escape",)),
    ),
    "question": (AnswerKey("Cancel", ("Escape",)),),
}
"""Claude Code 2.1.292's dialogs: a permission prompt's ``1. Yes``, ``2. Yes, and don't ask
again`` and Esc; a plan's three ways to go on and Esc to keep planning; a question's Esc,
after the digit of each option."""


class ClaudeTranscript:
    """Claude Code's answers to the transcript slot (``types.AgentTranscript``)."""

    speaker = SPEAKER

    def records(
        self, lines: Iterable[tuple[int, bytes]], ahead: Iterable[tuple[int, bytes]] = ()
    ) -> Iterator[tuple[int, dict[str, Any] | None]]:
        return one_per_line(lines)  # one whole record a line: none read ahead

    def tail_facts(self, file: Path, size: int, budget: int, written: datetime) -> TranscriptTail:
        return _tail_walk(file, size, budget, written)

    def page_lines(self, record: dict[str, Any], width: int) -> list[str]:
        return _render_transcript_record(record, width)

    def call_asks(self, tool: PendingTool) -> CallAsk:
        return _ASKS.get(tool.name, "tool")

    def call_mark(self, tool: PendingTool) -> str | None:
        """A question's words, a command, a file's base name, a URL: what Claude Code's
        prompt shows of the call; ``None`` for a call whose prompt is not known to."""
        if tool.name == "AskUserQuestion":
            return first_question(tool.input)
        if tool.name == "Bash":
            return _string(tool.input.get("command"))
        if tool.name in _FILE_TOOLS:
            path = _string(tool.input.get("file_path")) or _string(tool.input.get("notebook_path"))
            return path.replace("\\", "/").rsplit("/", 1)[-1]
        if tool.name == "WebFetch":
            return _string(tool.input.get("url"))
        return None

    def answer_keys(self, kind: AnswerKind, options: int = 0) -> tuple[AnswerKey, ...]:
        keys = _ANSWERS.get(kind, ())
        if kind != "question":
            return keys
        return (*(AnswerKey(str(n), (str(n),)) for n in range(1, options + 1)), *keys)

    def usage(self, file: Path, state: str | None) -> UsageScan:
        """Main's ``core.usage.scan``, moved to :mod:`claude_usage`."""
        from aisquare.core.agent_adapters import claude_usage

        return claude_usage.usage(file, state)

    def ledger(
        self, file: Path, *, since: datetime | None, until: datetime | None
    ) -> SessionLedger:
        """Main's token ledger reader (:mod:`claude_ledger`)."""
        from aisquare.core.agent_adapters import claude_ledger

        return claude_ledger.read(file, since=since, until=until)

    def context_tokens(self, file: Path) -> int | None:
        from aisquare.core.agent_adapters import claude_ledger

        return claude_ledger.context_tokens(file)

    def compacted_since(self, file: Path, since: datetime) -> bool:
        from aisquare.core.agent_adapters import claude_ledger

        return claude_ledger.compacted_since(file, since)

    def reply_since(self, file: Path, since: datetime | None) -> str | None:
        return last_reply(file, since=since)


# --- the reply the captain reads back: main's ``core.transcripts.last_reply``, moved ---------

#: How much of a transcript's tail :func:`last_reply` reads first.
_REPLY_TAIL_BYTES = 256_000
#: How far back :func:`last_reply` will read for the prompt a reply answers — a turn bigger
#: than this has lost its anchor, and says "no prompt" rather than guess.
_REPLY_MAX_BYTES = 64_000_000


def last_reply(
    path: Path,
    *,
    since: datetime | None = None,
    tail_bytes: int = _REPLY_TAIL_BYTES,
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
    try:
        size_now = path.stat().st_size
    except OSError:
        size_now = 0
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


def _tail_entries(path: Path, tail_bytes: int) -> Iterator[dict[str, Any]]:
    """The JSON entries of the last ``tail_bytes`` of ``path``, oldest first, a line the read
    began inside skipped; none for a file that cannot be read."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            end = handle.tell()
            start = max(end - tail_bytes, 0)
            handle.seek(start)
            raw = handle.read()
    except OSError:
        return
    lines = raw.split(b"\n")
    if start > 0:
        # The read began at an arbitrary byte: the first line is almost surely a partial one.
        lines = lines[1:]
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (ValueError, UnicodeDecodeError, RecursionError):
            continue
        if isinstance(entry, dict):
            yield entry


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _blocks(content: object) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


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


# --- the page: one record as terminal lines ---------------------------------------------------


def _render_tool_use(block: dict[str, Any], width: int) -> list[str]:
    """A tool call: one dim summary line, but a question or a plan IS the message.

    ``AskUserQuestion`` and ``ExitPlanMode`` are how an agent asks the human
    something, so they render in full — the questions and their options, the
    plan — and someone reading the transcript to answer sees what they answer.
    """
    name = block.get("name")
    payload = block.get("input")
    if name == "AskUserQuestion" and isinstance(payload, dict):
        asked = question_lines(payload.get("questions"), width)
        if asked:
            return asked
    if name == "ExitPlanMode" and isinstance(payload, dict):
        plan = payload.get("plan")
        if isinstance(plan, str) and plan.strip():
            return [f"{DIM}  ⎿ plan:{OFF}", *wrap(plan, width, indent="    ")]
    return [dim_note(_summarise_tool(block), width)]


def _render_transcript_record(record: dict[str, Any] | None, width: int) -> list[str]:
    """One transcript record as terminal lines, or ``[]`` when it is not conversation.

    A sub-agent's records (``isSidechain``) and Claude Code's own injected ones
    (``isMeta``: command caveats, skill bodies) are not the conversation either:
    a meta "user" record rendered as ``> you``, words the human never wrote. Nor
    is a compaction's summary (``isCompactSummary``): a "user" record without
    ``isMeta`` that holds kilobytes the model wrote about the conversation so far,
    which rendered as the human's words. It is one dim line saying the
    conversation was compacted, since the turns above it are no longer what the
    agent remembers. Nor are the other "user" records Claude Code writes itself
    (:func:`_render_claude_codes_own`).
    """
    if record is None or record.get("type") not in ("user", "assistant"):
        return []
    if record.get("isSidechain") is True or record.get("isMeta") is True:
        return []
    if record.get("isCompactSummary") is True:
        return [f"{DIM}  ⎿ conversation compacted{OFF}", ""]
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    role = message.get("role") or record.get("type")
    blocks = _blocks(message.get("content"))
    if not blocks:
        return []
    if role == "user":
        own = _render_claude_codes_own(record, blocks, width)
        if own is not None:
            return own

    body: list[str] = []
    for block in blocks:
        kind = block.get("type")
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                body.extend(wrap(text, width))
        elif kind == "tool_use":
            body.extend(_render_tool_use(block, width))
        # thinking and tool_result are deliberately not rendered — see the module
        # docstring. A user record carrying only tool_result therefore has no
        # body, and falls out below rather than being labelled as the person.
    if not body:
        return []

    # No time on the speaker's line: read_page sends it beside the lines (Page.stamps).
    return [YOU if role == "user" else speaker_line(SPEAKER), *body, ""]


_OWN_TEXT = re.compile(
    r"\s*<(command-|bash-|local-command|system-reminder|task-notification|user-prompt)[a-z-]*>"
)
"""How the text of a "user" record Claude Code writes itself begins: a slash command and its
output, a ``!`` command and its output, a background task's notice, a reminder, a hook's
words. Claude Code 2.1.295's own test of what is not a person's turn, to the tag's end."""

_OWN_OUTPUT = ("local-command-stdout", "bash-stdout", "local-command-stderr", "bash-stderr")
"""The tags a command's output comes in, the ones read first first."""


def _render_claude_codes_own(
    record: dict[str, Any], blocks: list[dict[str, Any]], width: int
) -> list[str] | None:
    """A "user" record Claude Code wrote itself, as lines; ``None`` for the human's words.

    Its ``origin`` names another source than a person (a background task's notice
    has ``{"kind": "task-notification"}``), or its text opens with one of Claude
    Code's tags (:data:`_OWN_TEXT`) and holds it closed: text a person typed that
    only starts like one is theirs, as typed. Rendered under ``> you``, a notice,
    often a sub-agent's whole report, read as the human's words, as the output of
    a ``/model`` did. The human's own command (``<command-name>``,
    ``<bash-input>``) is still theirs, without the tags; a notice or an output is
    one dim line, its summary or its first line, as is the first line of another
    source's words in no tag; a reminder or a hook's words are nothing.
    """
    text = "\n".join(
        part
        for block in blocks
        if block.get("type") == "text" and isinstance(part := block.get("text"), str)
    )
    origin = record.get("origin")
    foreign = isinstance(origin, dict) and origin.get("kind") not in (None, "human")
    opening = _OWN_TEXT.match(text)
    if not foreign:
        if opening is None or _own_tag(text, opening.group(0).strip()[1:-1]) is None:
            return None
        command = _own_tag(text, "command-name")
        if command:
            said = command if command.startswith("/") else "/" + command
            if args := _own_tag(text, "command-args"):
                said += " " + args
            return [YOU, *wrap(said, width), ""]
        if typed := _own_tag(text, "bash-input"):
            return [YOU, *wrap("! " + typed, width), ""]
    note = _own_tag(text, "summary") or next(
        (found for name in _OWN_OUTPUT if (found := _own_tag(text, name))), ""
    )
    if not note and (status := _own_tag(text, "status")):
        note = f"background task {status}"
    if not note and foreign and opening is None:
        note = text  # another source's words, in no tag
    first = next((line.strip() for line in note.splitlines() if line.strip()), "")
    if not first:
        return []
    return [dim_note(first, width), ""]


def _own_tag(text: str, name: str) -> str | None:
    """What the first ``<name>…</name>`` in ``text`` holds, stripped; ``None`` without one."""
    found = re.search(f"<{re.escape(name)}>(.*?)</{re.escape(name)}>", text, re.DOTALL)
    return None if found is None else found.group(1).strip()


# --- the tail: what the needs-you scan classifies on (SPEC §4.3) ---------------------------


def _tail_walk(file: Path, size: int, budget: int, written: datetime) -> TranscriptTail:
    """What the end of a Claude Code transcript says, free to raise ``OSError``.

    Newest record first, sub-agent (``isSidechain``) and injected (``isMeta``)
    records skipped, and the walk ends at whichever comes first: an assistant
    record of an OLDER message once the newest one has been seen, the human's
    own last prompt, :data:`TAIL_RECORDS`, or ``budget`` bytes. Every tool
    result met on the way answers its tool use, and a result may sit between two
    blocks of the newest message, because tools run while it streams. A line too
    long to parse is read for its ids alone (:func:`_tail_unparsed`): a result
    in it still answers, and a tool use in it still waits. Records without a
    ``message.id`` (an older Claude Code) are read back to the human's prompt
    instead. A walk that ran out before it got there says so (``cut_at``): what
    it did not read may hold a tool use still waiting.

    ``written`` is when the file last changed: the time of a newest record too
    long to parse, which its own ``timestamp`` cannot be read for, and of a walk cut
    short (``TranscriptTail.cut_at``).
    """
    answered: set[str] = set()
    tools: list[list[PendingTool]] = []  # each record's, newest record first
    texts: list[str] = []  # each record's text, newest record first
    newest, newest_at, marker_key = "none", None, None
    text_at: datetime | None = None
    message_id: str | None = None
    in_message = False  # the newest assistant message has been reached
    past_message = False  # ...and a user record older than it (records without an id)
    calls = False  # ...and a record of it calls a tool: an older one of it may too
    examined = 0
    cut = False  # the walk ran out before it could stop where nothing older waits
    own_only = True  # every record walked so far is one Claude Code wrote itself
    under_own = "none"
    under_own_at: datetime | None = None
    under_own_key: str | None = None
    for offset, raw in lines_backwards(file, size, budget=budget):
        if len(raw) > MAX_LINE:
            at = written if newest == "none" else None
            said, unparsed_tools = _tail_unparsed(raw, answered, at)
            if said is not None:
                tools.append(unparsed_tools)
                if newest == "none":
                    newest, newest_at, marker_key = said, at, str(offset)
                elif own_only:
                    under_own, under_own_key = said, str(offset)
                own_only = False
            continue
        record = parse_line(raw)
        if record is None or record.get("isSidechain") is True or record.get("isMeta") is True:
            continue
        message = record.get("message")
        if record.get("type") not in ("user", "assistant") or not isinstance(message, dict):
            continue
        examined += 1
        if examined > TAIL_RECORDS:
            cut = True
            break
        blocks = _blocks(message.get("content"))
        at = record_time(record)
        uuid = record.get("uuid")
        key = uuid if isinstance(uuid, str) and uuid else str(offset)
        if record.get("type") == "user":
            answered.update(
                block["tool_use_id"]
                for block in blocks
                if block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str)
            )
            said = _tail_user_kind(message.get("content"), blocks, record)
            if newest == "none":
                newest, newest_at, marker_key = said, at, key
            elif own_only and said != "own":
                under_own, under_own_at, under_own_key = said, at, key
            own_only = own_only and said == "own"
            if said == "user_prompt":
                break  # the human's own prompt: nothing before it waits on anyone
            if in_message:
                past_message = True
            continue
        found = message.get("id")
        found_id = found if isinstance(found, str) and found else None
        if not in_message:
            in_message, message_id = True, found_id
        elif message_id is not None and found_id != message_id:
            break  # an older message: every tool use in it has its result
        record_tools: list[PendingTool] = []
        record_text: list[str] = []
        for block in blocks:
            if block.get("type") == "tool_use":
                tool = _tail_pending(block, at)
                if tool is not None and tool.tool_use_id not in answered:
                    record_tools.append(tool)
            elif block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    record_text.append(text)
        tools.append(record_tools)
        # Without ids a message ends at the user record before it; with them, a
        # result between two of its blocks does not end it.
        if record_text and (message_id is not None or not past_message):
            texts.append("\n\n".join(record_text))
            text_at = text_at or at
        if any(block.get("type") == "tool_use" for block in blocks):
            said, calls = "assistant_tool", True
        elif record_text:
            said = "assistant_text"
        else:
            # A record of thinking alone is a message still streaming: the record
            # before it says what the agent is doing.
            continue
        if newest == "none":
            newest, newest_at, marker_key = said, at, key
        elif own_only:
            under_own, under_own_at, under_own_key = said, at, key
        own_only = False
    else:
        cut = size > budget  # the reader stopped on its budget, short of the file's start
    # The newest message is whole once an older one is met. Cut inside one that calls
    # tools, a block of it before the cut may be a tool use still waiting; one seen to
    # say only text ended its turn. An interruption ends the turn too: nothing before it
    # waits, however much was not read.
    unseen = not in_message or message_id is None or calls
    settled = under_own if newest == "own" and under_own != "none" else newest
    cut_at = written if cut and unseen and settled != "interrupted" else None
    pending: list[PendingTool] = []
    for record_tools in reversed(tools):
        for tool in record_tools:
            if all(tool.tool_use_id != kept.tool_use_id for kept in pending):
                pending.append(tool)
    return TranscriptTail(
        pending=tuple(pending),
        newest=newest,
        newest_at=newest_at,
        last_text="\n\n".join(reversed(texts)) if texts else None,
        last_text_at=text_at,
        marker_key=marker_key,
        cut_at=cut_at,
        under_own=under_own,
        under_own_at=under_own_at,
        under_own_key=under_own_key,
    )


def _tail_result_text(block: dict[str, Any]) -> str:
    """The text a ``tool_result`` block carries, as a string or as text blocks."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(part["text"] for part in _blocks(content) if isinstance(part.get("text"), str))


def _tail_user_kind(
    content: object, blocks: list[dict[str, Any]], record: dict[str, Any] | None = None
) -> str:
    """``interrupted``, ``tool_result``, ``own`` or ``user_prompt``: what a user record is.

    An interruption is Claude Code's marker text after an Esc, or the error
    result of a tool use the human rejected; either way the agent stopped and
    sits at its prompt. Not a rejection with words for the agent, after which
    it goes on working (:data:`REJECTED_WITH_WORDS`), nor a failed tool whose
    output only quotes the sentence (a test run of this very module): that is
    a result. ``own`` is a record Claude Code wrote itself (:func:`_tail_own`).
    Anything else that is not a tool result is the human.
    """
    texts = [content] if isinstance(content, str) else []
    texts += [
        b["text"] for b in blocks if b.get("type") == "text" and isinstance(b.get("text"), str)
    ]
    if any(text.lstrip().startswith(INTERRUPTED_MARKER) for text in texts):
        return "interrupted"
    results = [block for block in blocks if block.get("type") == "tool_result"]
    if any(_tail_rejected(block) for block in results):
        return "interrupted"
    if results:
        return "tool_result"
    return "own" if _tail_own(record or {}, "\n".join(texts)) else "user_prompt"


def _tail_own(record: dict[str, Any], text: str) -> bool:
    """Whether a "user" record that is no tool result is Claude Code's own, not a turn.

    A compaction's summary (``isCompactSummary``), another source's words (its
    ``origin``), or text in Claude Code's tags held closed (:data:`_OWN_TEXT`): a
    slash command and its output, a ``!`` command and its output, a notice, as the
    transcript renders them (:func:`_render_claude_codes_own`). A local command
    (``/model``, ``/mcp``, ``/compact``) runs no turn, so no hook follows it: taken for
    the human's prompt, it read as a turn under way, and an agent idle at its prompt
    was refused a typed message as busy for the board's 30 minutes (sweep 4 of #243).
    The walk goes on past such a record, as past a tool result.
    """
    if record.get("isCompactSummary") is True:
        return True
    origin = record.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return True
    opening = _OWN_TEXT.match(text)
    return opening is not None and _own_tag(text, opening.group(0).strip()[1:-1]) is not None


def _tail_rejected(block: dict[str, Any]) -> bool:
    """Whether a ``tool_result`` is a tool use the human turned down and left at that."""
    if block.get("is_error") is not True:
        return False
    text = _tail_result_text(block).lstrip()
    return text.startswith(REJECTED_MARKER) and REJECTED_WITH_WORDS not in text


def _tail_pending(block: dict[str, Any], at: datetime | None) -> PendingTool | None:
    """A ``tool_use`` block as a :class:`PendingTool`; ``None`` without an id to pair on."""
    tool_use_id = block.get("id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        return None
    name = block.get("name")
    kept, dropped = kept_input(block.get("input"))
    return PendingTool(
        tool_use_id=tool_use_id,
        name=name if isinstance(name, str) and name else "tool",
        summary=_summarise_tool(block),
        input=kept,
        at=at,
        input_dropped=dropped,
    )


def _tail_unparsed(
    raw: bytes, answered: set[str], at: datetime | None
) -> tuple[str | None, list[PendingTool]]:
    """A line too long to parse, read off its bytes: what record it is, and its pending tools.

    Its tool results answer their tool uses (``answered`` grows), and make it a
    ``tool_result``. A line with tool uses and no results is ``assistant_tool``,
    and each of its tool uses still without a result is pending, known by id and
    name alone: its input is not read, as a parsed one over ``TOOL_INPUT_MAX``
    is not kept. Skipped whole, a ``Write`` of a large file waited on its
    permission prompt unseen, and the text block before it in the same message
    read as the agent's last words at its prompt, where a typed message's Enter
    would have approved the write.
    ``None`` for a sub-agent's or an injected record, or one that shows neither.
    """
    results = [match.decode("utf-8", "replace") for match in _TOOL_USE_ID.findall(raw)]
    answered.update(results)
    if _UNPARSED_ASIDE.search(raw):
        return None, []
    if results:
        return "tool_result", []
    if not _TOOL_USE_BLOCK.search(raw):
        return None, []
    pending: list[PendingTool] = []
    for match in _TOOL_USE_BLOCK_ID.finditer(raw):
        tool_use_id = match.group(1).decode("utf-8", "replace")
        name = (match.group(2) or b"tool").decode("utf-8", "replace")
        if tool_use_id not in answered:
            pending.append(
                PendingTool(
                    tool_use_id=tool_use_id,
                    name=name,
                    summary=name,
                    input={},
                    at=at,
                    input_dropped=True,
                )
            )
    return "assistant_tool", pending
