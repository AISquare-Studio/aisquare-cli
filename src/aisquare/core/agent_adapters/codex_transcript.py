"""Codex's transcript, read for Remote: its session rollout
(``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<thread>.jsonl``, the ``transcript_path``
its hooks report). :class:`CodexTranscript` is Codex's answer to the transcript slot
(``AgentAdapter.transcript_reader``, ``types.AgentTranscript``).

A rollout is one record a line, ``{"timestamp", "type", "payload"}``. Measured on 0.162.1
with a loopback provider and the TUI under tmux (an approval asked and answered with ``1``
and with Esc, a ``request_user_input`` in Plan mode answered with a digit, a plan proposed):

- **The conversation** is the ``response_item`` records: a ``message`` (``role`` ``user``,
  ``assistant`` or ``developer``; ``content`` parts of ``input_text``/``output_text``), a
  call (``function_call`` with JSON ``arguments``, ``custom_tool_call`` with a freeform
  ``input``, ``local_shell_call`` with an ``action``), a call's result
  (``function_call_output``/``custom_tool_call_output``, paired by ``call_id``) and
  ``reasoning``. A ``user`` message that is Codex's own context (``<environment_context>``,
  ``<turn_aborted>``, AGENTS.md) is in one of its own tags held closed, as Claude Code's own
  records are.
  ``compacted`` records a compaction. The ``event_msg`` copies of these (``item_completed``,
  ``user_message``, ``agent_message``) are not read again.
- **A turn** opens with ``event_msg`` ``task_started`` and ends with ``task_complete``
  (its ``last_agent_message`` the agent's last words) or ``turn_aborted`` (``reason``
  ``interrupted``: an Esc, an approval or a question answered with Esc, after an ``aborted
  by user`` result for the call it stopped).
- **A model response** ends with a ``token_usage_record`` (0.162.1, written as the
  response completes) and an ``event_msg`` ``token_count`` (every version, written once
  its calls' results are in). The next response starts only once they are, so only the
  newest response can hold a call still waiting: the walk stops at the response before it.
- **An approval or a question waiting for the human leaves no record**: the call is in the
  rollout without its result until it is answered, as a call at work is. The board's
  attention (the PermissionRequest hook, and PreToolUse for ``request_user_input``) is what
  says a prompt is up, as Claude Code's Notification says it for a tool use.
- **The dialogs' keys**: an approval view's ``1. Yes, proceed``, ``2. Yes, and don't ask
  again…`` and ``3. No… (esc)`` take their digit, and Esc stops the turn; a question's
  options take their digit (one question: picked and submitted), Esc stops the turn.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Mapping
from contextlib import suppress
from datetime import datetime
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

SPEAKER = "codex"

_CALLS = frozenset({"function_call", "custom_tool_call", "local_shell_call"})
"""The ``response_item`` types that call a tool, each paired with its result by ``call_id``."""
_RESULTS = frozenset({"function_call_output", "custom_tool_call_output"})
_RESPONSE_ENDS = frozenset({"token_count"})
"""The ``event_msg`` types that close a model response (and ``token_usage_record``)."""

_QUESTION_TOOL = "request_user_input"
"""Codex's tool for asking the human (Plan mode only, 0.162.1): ``questions`` of ``id``,
``header``, ``question`` and ``options`` of ``label`` and ``description``."""

_OWN_TAGS = (
    "environment_context",
    "turn_aborted",
    "user_action",
    "recommended_plugins",
    "user_shell_command",
    "subagent_notification",
    "skill",
    "user_instructions",
)
"""The tags Codex wraps the context it writes as a ``user`` message in. Measured in 0.162.1
rollouts: ``environment_context``, ``turn_aborted``, ``user_action`` (a ``/review``'s
results) and ``recommended_plugins``. Named beside them in its binary: ``user_shell_command``
(a ``!`` command), ``subagent_notification`` and ``skill``; ``user_instructions`` is an
earlier version's AGENTS.md. Any other tag is the human's (review r16 of #113, 1/8)."""
_OWN_TEXT = re.compile(rf"\s*<({'|'.join(_OWN_TAGS)})>")
"""How a ``user`` message Codex writes itself begins: one of :data:`_OWN_TAGS`, held closed
(:func:`_own`)."""
_INSTRUCTIONS = "# AGENTS.md instructions for "
"""How the AGENTS.md Codex hands the model as a ``user`` message begins."""

_PROPOSED_PLAN = re.compile(r"<proposed_plan>\s*(.*?)\s*</proposed_plan>", re.DOTALL)
"""A plan Codex proposes in Plan mode, inside its final message (0.162.1)."""

# Read off the bytes of a line too long to parse: the record's own keys, never text quoted
# in a string (a ``\"`` inside one never matches).
_RAW_TYPE = re.compile(rb'"type"\s*:\s*"([a-z_]+)"')
_RAW_CALL_ID = re.compile(rb'"call_id"\s*:\s*"([^"\\]{1,256})"')
_RAW_NAME = re.compile(rb'"name"\s*:\s*"([^"\\]{1,64})"')
_RAW_ROLE_ASSISTANT = re.compile(rb'"role"\s*:\s*"assistant"')
_RAW_ROLE_USER = re.compile(rb'"role"\s*:\s*"user"')
_RAW_TEXT = re.compile(rb'"text"\s*:\s*"([^"]{0,64})')
_RAW_HEAD = 1024
"""How much of a long line's start holds its ``type`` keys: the record's and its payload's
come before any content (``{"timestamp", "type", "payload": {"type", …}}``)."""

_ANSWERS: Mapping[AnswerKind, tuple[AnswerKey, ...]] = {
    "permission": (AnswerKey("1", ("1",)), AnswerKey("2", ("2",)), AnswerKey("No", ("Escape",))),
    "question": (AnswerKey("Cancel", ("Escape",)),),
}
"""Codex 0.162.1's approval view (``1. Yes, proceed``, ``2. Yes, and don't ask again``, Esc
for ``No``, which stops the turn) and question view (each option's digit, then Esc, which
stops the turn); it has no plan call to approve."""


class CodexTranscript:
    """Codex's answers to the transcript slot (``types.AgentTranscript``)."""

    speaker = SPEAKER

    def records(
        self, lines: Iterable[tuple[int, bytes]], ahead: Iterable[tuple[int, bytes]] = ()
    ) -> Iterator[tuple[int, dict[str, Any] | None]]:
        return one_per_line(lines)  # one whole record a line: none read ahead

    def tail_facts(self, file: Path, size: int, budget: int, written: datetime) -> TranscriptTail:
        return _rollout_tail(file, size, budget, written)

    def page_lines(self, record: dict[str, Any], width: int) -> list[str]:
        return _rollout_lines(record, width)

    def call_asks(self, tool: PendingTool) -> CallAsk:
        return "question" if tool.name == _QUESTION_TOOL else "tool"

    def call_mark(self, tool: PendingTool) -> str | None:
        """The command an approval view shows (``$ <cmd>``), or a question's words."""
        if tool.name == _QUESTION_TOOL:
            return first_question(tool.input)
        command = tool.input.get("cmd", tool.input.get("command"))
        if isinstance(command, list):
            command = " ".join(part for part in command if isinstance(part, str))
        return command if isinstance(command, str) and command else None

    def answer_keys(self, kind: AnswerKind, options: int = 0) -> tuple[AnswerKey, ...]:
        keys = _ANSWERS.get(kind, ())
        if kind != "question":
            return keys
        return (*(AnswerKey(str(n), (str(n),)) for n in range(1, options + 1)), *keys)

    def usage(self, file: Path, state: str | None) -> UsageScan:
        """Its ``token_usage_record`` lines (:mod:`codex_usage`)."""
        from aisquare.core.agent_adapters import codex_usage

        return codex_usage.usage(file, state)

    def reply_since(self, file: Path, since: datetime | None) -> str | None:
        return _rollout_reply(file, since)


_REPLY_MAX_BYTES = 64_000_000
"""How far back :func:`_rollout_reply` reads for the prompt a reply answers, as Claude Code's
reader does: a turn bigger than this has lost its anchor, and says "no prompt"."""
_RAW_TIMESTAMP = re.compile(rb'\A\{"timestamp"\s*:\s*"([^"\\]{1,64})"')
"""A record's time off the bytes of a line too long to parse: its first key."""


def _rollout_reply(file: Path, since: datetime | None) -> str | None:
    """The agent's words since the rollout's newest prompt of the human's (``AgentTranscript.
    reply_since``): each assistant ``message`` after it, oldest first, joined by newlines.

    The prompt is the newest ``user`` message that is not Codex's own context (:func:`_own`:
    its ``<turn_aborted>``, ``<environment_context>``, AGENTS.md), so a turn the human
    interrupted keeps its prompt. A call (``function_call`` and the rest) answers it too,
    with no words, as a tool use does on Claude Code. The ``event_msg`` copies of the
    conversation (``agent_message``, ``item_completed``) are not read again. Read backwards
    from the end up to :data:`_REPLY_MAX_BYTES`; a line too long to parse is read for its
    kind (a long message of the agent's is parsed all the same: it is the reply)."""
    try:
        size = file.stat().st_size
    except OSError:
        return None
    words: list[str] = []
    answered = False
    try:
        for _, raw in lines_backwards(file, size, budget=_REPLY_MAX_BYTES):
            if len(raw) > MAX_LINE:
                kind, _ = _rollout_unparsed(raw, set(), None)
                if kind == "user_prompt":
                    stamp = _RAW_TIMESTAMP.match(raw)
                    when = stamp.group(1).decode("utf-8", "replace") if stamp else None
                    return _reply(words, answered, record_time({"timestamp": when}), since)
                if kind == "assistant_text":
                    with suppress(ValueError, UnicodeDecodeError, RecursionError):
                        words.insert(0, _message_text(_payload(json.loads(raw))))
                answered = answered or kind in ("assistant_text", "assistant_tool")
                continue
            record = parse_line(raw)
            if record is None or record.get("type") != "response_item":
                continue
            payload = _payload(record)
            kind = payload.get("type")
            if kind in _CALLS:
                answered = True
            elif kind == "message" and payload.get("role") == "assistant":
                answered = True
                text = _message_text(payload)
                if text.strip():
                    words.insert(0, text)
            elif kind == "message" and payload.get("role") == "user":
                if _own(_message_text(payload).lstrip()):
                    continue
                return _reply(words, answered, record_time(record), since)
    except OSError:
        return None
    return None


def _reply(
    words: list[str], answered: bool, asked_at: datetime | None, since: datetime | None
) -> str | None:
    if since is not None and asked_at is not None and asked_at < since:
        return None  # an older turn's prompt: the reply has not reached the rollout yet
    return "\n".join(words).strip() if answered else None


def _payload(record: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else {}


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _message_text(payload: Mapping[str, Any]) -> str:
    """A ``message``'s text parts, joined."""
    content = payload.get("content")
    if isinstance(content, str):
        return content
    parts = content if isinstance(content, list) else []
    return "\n".join(
        part["text"]
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


def _own(text: str) -> bool:
    """Whether a ``user`` message is Codex's own context, not the human's words: one of its
    tags held closed (:data:`_OWN_TEXT`), or the AGENTS.md it passes on. Text a person typed
    that only starts like one is theirs, as typed, and so is a prompt that opens with a tag
    of its own (``<task>…</task> …``): taken for context, it was missing from the Transcript
    tab, and the tail read ``own`` until the agent answered (review r16 of #113, 1/8)."""
    if text.startswith(_INSTRUCTIONS):
        return True
    opening = _OWN_TEXT.match(text)
    return opening is not None and f"</{opening.group(1)}>" in text


def _call_id(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("call_id")
    return value if isinstance(value, str) and value else None


def _call_name(payload: Mapping[str, Any]) -> str:
    if payload.get("type") == "local_shell_call":
        return "local_shell"
    name = payload.get("name")
    return name if isinstance(name, str) and name else "tool"


def _call_input(payload: Mapping[str, Any]) -> object:
    """A call's input as an object: ``function_call``'s JSON ``arguments``, a
    ``custom_tool_call``'s freeform ``input`` (a patch, a script) as ``{"input": …}``, a
    ``local_shell_call``'s ``action`` command as ``{"command": …}``."""
    kind = payload.get("type")
    if kind == "custom_tool_call":
        return {"input": _string(payload.get("input"))}
    if kind == "local_shell_call":
        action = payload.get("action")
        command = action.get("command") if isinstance(action, dict) else None
        if isinstance(command, list):
            return {"command": " ".join(part for part in command if isinstance(part, str))}
        return {}
    arguments = payload.get("arguments")
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed: Any = json.loads(arguments) if isinstance(arguments, str) else None
    except (ValueError, RecursionError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _summary(name: str, arguments: object) -> str:
    """A call as one line: its name, and the first line of what it runs or asks."""
    detail = ""
    if isinstance(arguments, dict):
        for key in ("cmd", "command", "input", "path", "query", "url", "prompt"):
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                detail = value.strip().splitlines()[0]
                break
        else:
            asked = first_question(arguments)
            detail = asked.strip().splitlines()[0] if asked.strip() else ""
    return f"{name}({detail[:72]})" if detail else name


def _pending(payload: Mapping[str, Any], call_id: str, at: datetime | None) -> PendingTool:
    name = _call_name(payload)
    arguments = _call_input(payload)
    kept, dropped = kept_input(arguments)
    return PendingTool(
        tool_use_id=call_id,
        name=name,
        summary=_summary(name, arguments),
        input=kept,
        at=at,
        input_dropped=dropped,
    )


def _key(file: Path, offset: int) -> str:
    """A record's key: where it is in the rollout, which only ever grows. The rollout's name
    holds its thread's id, so two agents' records at the same offset are two keys."""
    return f"{file.name}:{offset}"


def _rollout_tail(file: Path, size: int, budget: int, written: datetime) -> TranscriptTail:
    """What the end of a Codex rollout says, free to raise ``OSError``.

    Newest record first, and the walk ends at whichever comes first: the human's own last
    prompt or the turn's ``task_started``, the end of the response before the newest one
    (once an item of the newest has been met: every call of the one before has its
    result), ``TAIL_RECORDS`` conversation records, or ``budget`` bytes. A turn that ended
    (its ``task_complete`` or ``turn_aborted`` met before anything of it but Codex's own
    records) waits on nothing: the walk reads back only to the agent's last words, and is
    never cut. Every result met answers its call; a call met without one is pending. A
    line too long to parse is read for its ids alone (:func:`_rollout_unparsed`). A walk
    that ran out inside the newest response of a turn still running says so (``cut_at``):
    a call of it may wait unseen.
    """
    answered: set[str] = set()
    pending: list[PendingTool] = []  # newest first
    newest, newest_at, marker_key = "none", None, None
    last_text: str | None = None
    text_at: datetime | None = None
    spoke = False  # the agent's newest message has been met, whether or not it was read
    ended = False  # the newest turn's end was met before any record of it
    final: str | None = None  # what that end says the agent said last
    in_response = False  # an item of the newest response has been met
    examined = 0
    cut = False  # the walk ran out before it could stop where nothing older waits
    own_only = True  # every record walked so far is one Codex wrote itself
    under_own = "none"
    under_own_at: datetime | None = None
    under_own_key: str | None = None

    def said(kind: str, at: datetime | None, key: str) -> None:
        nonlocal newest, newest_at, marker_key, own_only, under_own, under_own_at, under_own_key
        if newest == "none":
            newest, newest_at, marker_key = kind, at, key
        elif own_only and kind != "own":
            under_own, under_own_at, under_own_key = kind, at, key
        own_only = own_only and kind == "own"

    for offset, raw in lines_backwards(file, size, budget=budget):
        if ended and spoke:
            break  # a turn that ended, its last words met: nothing older changes the tail
        key = _key(file, offset)
        if len(raw) > MAX_LINE:
            at = written if newest == "none" else None
            kind, call = _rollout_unparsed(raw, answered, at)
            if kind == "ended":
                ended = ended or own_only
            elif kind is not None:
                examined += 1
                in_response = in_response or kind in ("assistant_text", "assistant_tool")
                spoke = spoke or kind == "assistant_text"
                if call is not None and not ended:
                    pending.append(call)
                said(kind, at, key)
                if kind == "user_prompt":
                    break
            continue
        record = parse_line(raw)
        if record is None:
            continue
        top = record.get("type")
        payload = _payload(record)
        kind = payload.get("type")
        at = record_time(record)
        if top == "event_msg":
            if kind in ("task_complete", "turn_aborted") and own_only:
                ended = True
                if kind == "turn_aborted":
                    examined += 1
                    said("interrupted", at, key)
                else:
                    found = payload.get("last_agent_message")
                    final = found if isinstance(found, str) and found.strip() else None
            elif kind == "task_started":
                break  # the turn's start: nothing older waits on anyone
            elif kind in _RESPONSE_ENDS and in_response:
                break  # the response before the newest: every call of it has its result
            continue
        if top == "token_usage_record":
            if in_response:
                break
            continue
        if top == "compacted":
            examined += 1
            said("own", at, key)
            continue
        if top != "response_item":
            continue
        examined += 1
        if examined > TAIL_RECORDS:
            cut = True
            break
        if kind in _RESULTS:
            call_id = _call_id(payload)
            if call_id is not None:
                answered.add(call_id)
            said("tool_result", at, key)
        elif kind in _CALLS:
            in_response = True
            call_id = _call_id(payload)
            if call_id is not None and call_id not in answered and not ended:
                pending.append(_pending(payload, call_id, at))
            said("assistant_tool", at, key)
        elif kind == "reasoning":
            in_response = True  # thinking alone: the record before it says what it does
        elif kind == "message" and payload.get("role") == "assistant":
            in_response = True
            text = _message_text(payload)
            if text.strip():
                said("assistant_text", at, key)
                if not spoke:
                    spoke, last_text, text_at = True, text, at
        elif kind == "message" and payload.get("role") == "user":
            if _own(_message_text(payload).lstrip()):
                said("own", at, key)
                continue
            said("user_prompt", at, key)
            break  # the human's own prompt: nothing before it waits on anyone
    else:
        cut = size > budget  # the reader stopped on its budget, short of the file's start
    if last_text is None and final is not None:
        last_text, text_at = final, newest_at
    return TranscriptTail(
        pending=() if ended else tuple(reversed(pending)),
        newest=newest,
        newest_at=newest_at,
        last_text=last_text,
        last_text_at=text_at,
        marker_key=marker_key,
        cut_at=written if cut and not ended else None,
        under_own=under_own,
        under_own_at=under_own_at,
        under_own_key=under_own_key,
    )


def _rollout_unparsed(
    raw: bytes, answered: set[str], at: datetime | None
) -> tuple[str | None, PendingTool | None]:
    """A line too long to parse, read off its bytes: what record it is, and its call when it
    is one still without a result (known by id and name alone, its input not read).

    ``ended`` for a turn's ``task_complete`` (its last words too long to parse); a result
    answers its call (``answered`` grows); ``None`` for a record the tail does not read.
    """
    head = raw[:_RAW_HEAD]
    types = [match.decode() for match in _RAW_TYPE.findall(head)[:2]]
    top = types[0] if types else ""
    kind = types[1] if len(types) > 1 else ""
    if top == "compacted":
        return "own", None
    if top == "event_msg":
        return ("ended", None) if kind == "task_complete" else (None, None)
    if top != "response_item":
        return None, None
    found = _RAW_CALL_ID.search(raw)
    call_id = found.group(1).decode("utf-8", "replace") if found else None
    if kind in _RESULTS:
        if call_id is not None:
            answered.add(call_id)
        return "tool_result", None
    if kind in _CALLS:
        if call_id is None or call_id in answered:
            return "assistant_tool", None
        named = _RAW_NAME.search(head)
        name = named.group(1).decode("utf-8", "replace") if named else "tool"
        call = PendingTool(call_id, name, name, {}, at, input_dropped=True)
        return "assistant_tool", call
    if kind == "message":
        if _RAW_ROLE_ASSISTANT.search(head):
            return "assistant_text", None
        if _RAW_ROLE_USER.search(head):
            opening = _RAW_TEXT.search(head)
            words = opening.group(1).decode("utf-8", "replace").lstrip() if opening else ""
            own = words.startswith(_INSTRUCTIONS) or _OWN_TEXT.match(words) is not None
            return ("own" if own else "user_prompt"), None
    return None, None


# --- the page: one record as terminal lines ---------------------------------------------------


def _rollout_lines(record: dict[str, Any], width: int) -> list[str]:
    """One rollout record as terminal lines, or ``[]`` when it is not conversation: the
    human's words, the agent's (a proposed plan in full), each call as one dim line (a
    question in full), an interruption and a compaction as a dim note. Results, reasoning,
    Codex's own context and the ``event_msg`` copies of the conversation are not shown."""
    top = record.get("type")
    payload = _payload(record)
    kind = payload.get("type")
    if top == "compacted":
        return [f"{DIM}  ⎿ conversation compacted{OFF}", ""]
    if top == "event_msg":
        if kind == "turn_aborted":
            return [dim_note("interrupted", width), ""]
        return []
    if top != "response_item":
        return []
    if kind == "message":
        role = payload.get("role")
        text = _message_text(payload)
        if not text.strip():
            return []
        if role == "user":
            return [] if _own(text.lstrip()) else [YOU, *wrap(text, width), ""]
        if role == "assistant":
            return [speaker_line(SPEAKER), *_said(text, width), ""]
        return []
    if kind in _CALLS:
        name = _call_name(payload)
        arguments = _call_input(payload)
        if name == _QUESTION_TOOL and isinstance(arguments, dict):
            asked = question_lines(arguments.get("questions"), width)
            if asked:
                return [speaker_line(SPEAKER), *asked, ""]
        return [speaker_line(SPEAKER), dim_note(_summary(name, arguments), width), ""]
    return []


def _said(text: str, width: int) -> list[str]:
    """The agent's words, a ``<proposed_plan>`` in them as a plan: what is around it as
    words, the plan under a dim ``plan:`` line, indented."""
    lines: list[str] = []
    rest = text
    while (found := _PROPOSED_PLAN.search(rest)) is not None:
        before = rest[: found.start()].strip()
        if before:
            lines.extend(wrap(before, width))
        lines.extend([f"{DIM}  ⎿ plan:{OFF}", *wrap(found.group(1), width, indent="    ")])
        rest = rest[found.end() :]
    if rest.strip():
        lines.extend(wrap(rest.strip() if lines else rest, width))
    return lines
