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

**The tail** (:func:`read_transcript_tail`) is the other reader: not lines for a
person but the facts the needs-you scan classifies on (SPEC §4.3) — the tool
uses still waiting on an answer, what the newest record is (an interruption,
the agent's own words, a tool result), and the newest assistant text. Claude
Code starts no assistant message until every tool result of the one before is
in, so only the newest message can hold a pending tool use, and the walk stops
at the message before it: a working agent costs its newest message, not its
whole history.
"""

from __future__ import annotations

import json
import re
import stat
import textwrap
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
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

TAIL_BUDGET = 524_288
"""Most bytes :func:`read_transcript_tail` reads off the end of a transcript."""

TAIL_RECORDS = 400
"""Most conversation records the tail walk examines: a backstop, not the usual bound."""

TOOL_INPUT_MAX = 16_384
"""Most bytes of a pending tool's input (as JSON) a :class:`PendingTool` keeps; more is ``{}``."""

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

    collected: list[tuple[list[str], str | None]] = []
    oldest = end
    reached_start = False
    try:
        for offset, raw in _lines_backwards(file, end):
            oldest = offset
            if offset == 0:
                reached_start = True
            record = _parse_transcript_line(raw)
            rendered = _render_transcript_record(record, width)
            if record is not None and rendered:
                collected.append((rendered, _stamp(record)))
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


def _blocks(content: object) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def _stamp(record: dict[str, Any]) -> str | None:
    """When ``record`` was written, as ISO UTC for :attr:`Page.stamps`; ``None`` without one.

    Never a clock time: rendered here, ``astimezone()`` is the MACHINE's zone, and a fleet
    on a UTC box read from a phone in UTC-7 said ``> you 17:05`` for a prompt typed at
    10:05 by the phone's clock, on a page that tells every other time in the phone's own
    zone. Read as :func:`_tail_time` reads it, a time without a zone being UTC (Claude
    Code writes ``Z``): this read it as the machine's local time.
    """
    when = _tail_time(record)
    return None if when is None else when.astimezone(UTC).isoformat(timespec="seconds")


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


def _render_questions(questions: object, width: int) -> list[str]:
    """An ``AskUserQuestion``'s questions as the human must read them to answer.

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
        lines.extend(_wrap(line, width))
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
            lines.extend(_wrap(entry, width, indent="    "))
    return lines


def _render_tool_use(block: dict[str, Any], width: int) -> list[str]:
    """A tool call: one dim summary line, but a question or a plan IS the message.

    ``AskUserQuestion`` and ``ExitPlanMode`` are how an agent asks the human
    something, so they render in full — the questions and their options, the
    plan — and someone reading the transcript to answer sees what they answer.
    """
    name = block.get("name")
    payload = block.get("input")
    if name == "AskUserQuestion" and isinstance(payload, dict):
        asked = _render_questions(payload.get("questions"), width)
        if asked:
            return asked
    if name == "ExitPlanMode" and isinstance(payload, dict):
        plan = payload.get("plan")
        if isinstance(plan, str) and plan.strip():
            return [f"{_DIM}  ⎿ plan:{_OFF}", *_wrap(plan, width, indent="    ")]
    return [f"{_DIM}  ⎿ {_summarise_tool(block)}{_OFF}"]


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
        return [f"{_DIM}  ⎿ conversation compacted{_OFF}", ""]
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
                body.extend(_wrap(text, width))
        elif kind == "tool_use":
            body.extend(_render_tool_use(block, width))
        # thinking and tool_result are deliberately not rendered — see the module
        # docstring. A user record carrying only tool_result therefore has no
        # body, and falls out below rather than being labelled as the person.
    if not body:
        return []

    # No time on the speaker's line: read_page sends it beside the lines (Page.stamps).
    return [_SPEAKERS["user" if role == "user" else "assistant"], *body, ""]


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
            return [_SPEAKERS["user"], *_wrap(said, width), ""]
        if typed := _own_tag(text, "bash-input"):
            return [_SPEAKERS["user"], *_wrap("! " + typed, width), ""]
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
    room = max(1, width - 4)
    shown = first if len(first) <= room else first[: room - 1] + "…"
    return [f"{_DIM}  ⎿ {shown}{_OFF}", ""]


def _own_tag(text: str, name: str) -> str | None:
    """What the first ``<name>…</name>`` in ``text`` holds, stripped; ``None`` without one."""
    found = re.search(f"<{re.escape(name)}>(.*?)</{re.escape(name)}>", text, re.DOTALL)
    return None if found is None else found.group(1).strip()


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


@dataclass(frozen=True)
class TranscriptTail:
    """The end of one conversation, reduced to what decides whether it needs the human."""

    pending: tuple[PendingTool, ...]
    """Tool uses still waiting for their result, OLDEST first."""
    newest: str
    """The newest record: ``assistant_text``, ``assistant_tool``, ``user_prompt``,
    ``tool_result``, ``interrupted`` (an Esc, or a rejected tool use) or ``none``."""
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


_TAIL_NOTHING = TranscriptTail(
    pending=(),
    newest="none",
    newest_at=None,
    last_text=None,
    last_text_at=None,
    marker_key=None,
    empty=True,
)
"""The tail of a transcript with no record in it yet."""


def read_transcript_tail(
    path: Path | str | None, *, budget: int = TAIL_BUDGET
) -> TranscriptTail | None:
    """The tail of the transcript at ``path``; ``None`` when there is none to read.

    Newest record first, sub-agent (``isSidechain``) and injected (``isMeta``)
    records skipped, and the walk ends at whichever comes first: an assistant
    record of an OLDER message once the newest one has been seen, the human's
    own last prompt, :data:`TAIL_RECORDS`, or ``budget`` bytes. Every tool
    result met on the way answers its tool use, and a result may sit between two
    blocks of the newest message, because tools run while it streams. A line too
    long to parse is read for its ids alone (:func:`_tail_unparsed`): a result
    in it still answers, and a tool use in it still waits. Records without a
    ``message.id`` (an older Claude Code) are read back to the human's prompt
    instead. Never raises: an unreadable file is ``None``.

    Only a regular file is opened. The path is whatever the agent's own hook
    payload said, and opening a named pipe waits for a writer: the watcher's
    scan would wait with it, every later scan behind it, and so would an action
    re-reading that agent's project. An empty file is no conversation yet,
    answered without opening it, as :func:`read_page` answers it.
    """
    if path is None:
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
        return _tail_walk(file, facts.st_size, budget, written)
    except OSError:
        return None


def _tail_walk(file: Path, size: int, budget: int, written: datetime) -> TranscriptTail:
    """The body of :func:`read_transcript_tail`, free to raise ``OSError``.

    ``written`` is when the file last changed: the time of a newest record too
    long to parse, which its own ``timestamp`` cannot be read for.
    """
    answered: set[str] = set()
    tools: list[list[PendingTool]] = []  # each record's, newest record first
    texts: list[str] = []  # each record's text, newest record first
    newest, newest_at, marker_key = "none", None, None
    text_at: datetime | None = None
    message_id: str | None = None
    in_message = False  # the newest assistant message has been reached
    past_message = False  # ...and a user record older than it (records without an id)
    examined = 0
    for offset, raw in _lines_backwards(file, size, budget=budget):
        if len(raw) > MAX_LINE:
            at = written if newest == "none" else None
            said, unparsed_tools = _tail_unparsed(raw, answered, at)
            if said is not None:
                tools.append(unparsed_tools)
                if newest == "none":
                    newest, newest_at, marker_key = said, at, str(offset)
            continue
        record = _parse_transcript_line(raw)
        if record is None or record.get("isSidechain") is True or record.get("isMeta") is True:
            continue
        message = record.get("message")
        if record.get("type") not in ("user", "assistant") or not isinstance(message, dict):
            continue
        examined += 1
        if examined > TAIL_RECORDS:
            break
        blocks = _blocks(message.get("content"))
        at = _tail_time(record)
        uuid = record.get("uuid")
        key = uuid if isinstance(uuid, str) and uuid else str(offset)
        if record.get("type") == "user":
            answered.update(
                block["tool_use_id"]
                for block in blocks
                if block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str)
            )
            said = _tail_user_kind(message.get("content"), blocks)
            if newest == "none":
                newest, newest_at, marker_key = said, at, key
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
        if newest == "none":
            if any(block.get("type") == "tool_use" for block in blocks):
                newest, newest_at, marker_key = "assistant_tool", at, key
            elif record_text:
                newest, newest_at, marker_key = "assistant_text", at, key
            # A record of thinking alone is a message still streaming: the record
            # before it says what the agent is doing.
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
    )


def _tail_time(record: dict[str, Any]) -> datetime | None:
    """The record's ``timestamp`` as an aware datetime; ``None`` when it has none."""
    raw = record.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _tail_result_text(block: dict[str, Any]) -> str:
    """The text a ``tool_result`` block carries, as a string or as text blocks."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(part["text"] for part in _blocks(content) if isinstance(part.get("text"), str))


def _tail_user_kind(content: object, blocks: list[dict[str, Any]]) -> str:
    """``interrupted``, ``tool_result`` or ``user_prompt``: what a user record is.

    An interruption is Claude Code's marker text after an Esc, or the error
    result of a tool use the human rejected; either way the agent stopped and
    sits at its prompt. Not a rejection with words for the agent, after which
    it goes on working (:data:`REJECTED_WITH_WORDS`), nor a failed tool whose
    output only quotes the sentence (a test run of this very module): that is
    a result. Anything else that is not a tool result is the human.
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
    return "tool_result" if results else "user_prompt"


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
    payload = block.get("input")
    kept: Mapping[str, object] = {}
    if isinstance(payload, dict):
        try:
            size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8", "replace"))
        except (ValueError, RecursionError):  # what parsed may still be too deep to write
            size = TOOL_INPUT_MAX + 1
        kept = payload if size <= TOOL_INPUT_MAX else {}
    return PendingTool(
        tool_use_id=tool_use_id,
        name=name if isinstance(name, str) and name else "tool",
        summary=_summarise_tool(block),
        input=kept,
        at=at,
    )


def _tail_unparsed(
    raw: bytes, answered: set[str], at: datetime | None
) -> tuple[str | None, list[PendingTool]]:
    """A line too long to parse, read off its bytes: what record it is, and its pending tools.

    Its tool results answer their tool uses (``answered`` grows), and make it a
    ``tool_result``. A line with tool uses and no results is ``assistant_tool``,
    and each of its tool uses still without a result is pending, known by id and
    name alone: its input is not read, as a parsed one over :data:`TOOL_INPUT_MAX`
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
                PendingTool(tool_use_id=tool_use_id, name=name, summary=name, input={}, at=at)
            )
    return "assistant_tool", pending


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
    "Page",
    "PendingTool",
    "TranscriptTail",
    "read_page",
    "read_transcript_tail",
]
