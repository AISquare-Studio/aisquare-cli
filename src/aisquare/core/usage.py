"""Token usage of a Claude Code session, read from its transcript INCREMENTALLY.

The transcript is the only exact record there is: every API request Claude
Code makes leaves an ``assistant`` entry carrying the request's
``message.usage``. :func:`scan` reads it from where the previous scan stopped
(a byte offset per file, kept in :class:`ScanState`) and returns what the new
bytes add, so a Stop hook costs the turn it closes and not the session's
history. Shapes, read off this machine's transcripts (Claude Code 2.1.309)::

    {"type": "assistant", "message": {"id": "msg_…", "model": "claude-opus-5-5",
        "content": [{"type": "tool_use", "id": "toolu_…", "name": "Bash",
                     "input": {"command": "…"}}],
        "usage": {"input_tokens": 2, "cache_creation_input_tokens": 2183,
                  "cache_read_input_tokens": 43431, "output_tokens": 136,
                  "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                     "ephemeral_1h_input_tokens": 2183}}}}
    {"type": "user", "message": {"content": [{"type": "tool_result",
        "tool_use_id": "toolu_…", "content": "…"}]}}
    {"type": "attachment", "attachment": {"type": "hook_success",
        "hookEvent": "SessionStart", "command": "…/asq hook session-start",
        "content": "<aisquare-context>…"}}

**One request, several lines.** A streamed reply is written as one line per
content block, every one carrying the same ``message.id`` and the same usage.
Summing lines counts a request two or three times; usage is counted once per
id, and should a later line of the same id carry a larger figure (an earlier
build wrote ``output_tokens`` as it grew) only the increase is added. The ids
already counted are remembered across scans (:data:`_RECENT_IDS` of them),
because a request's lines can straddle two Stops.

**Subagents are part of the session.** A ``Task`` subagent's requests are
written to ``<session>/subagents/agent-*.jsonl`` beside the transcript, not
into it; they are billed all the same, so they are read too, and the entries
of all the files are merged in timestamp order so that "before the first
edit" means before it in time, whichever agent made the edit.

**Reading never raises.** A missing file, a truncated line, a non-JSON line or
an entry of an unknown shape costs that entry and nothing else; a final line
without its newline is not consumed, so a reply still being written is read
whole by the next scan rather than half by this one.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aisquare.core import pricing
from aisquare.core.pricing import RequestTokens

#: The tools whose first call ends a session's exploration: the agent has found
#: where to change and is changing it.
EDIT_TOOLS = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})

#: The tool that starts a subagent: ``Task`` in Claude Code's tool list, ``Agent``
#: in the calls it writes to the transcript (2.1.309, measured).
SUBAGENT_TOOLS = frozenset({"Task", "Agent"})

#: The hook events whose output Claude Code puts in front of the model. The
#: others' stdout is bookkeeping (``Stop``'s is a decision, read apart below).
_CONTEXT_EVENTS = frozenset({"SessionStart", "UserPromptSubmit", "PostToolUse", "PreToolUse"})

#: How many message ids and tool-use ids a session remembers between scans. A
#: request's lines are adjacent in the file; this only has to outlast a Stop
#: falling between two of them, and a subagent file racing the main one.
_RECENT_IDS = 512

#: The markers aisquare's own context is framed in. Seen anywhere but a tool's
#: OUTPUT, they mean aisquare reached the model (a tool result may be a file of
#: this repository that merely names them).
AISQUARE_MARKERS = ("<aisquare-context>", "<aisquare-team", "<aisquare-team-delta>")

_AISQUARE_HOOK = re.compile(
    r"aisquare-hook\b"  # the Claude Code plugin's script
    r"|(?:^|[\s/\\\"'])(?:aisquare|asq)(?:\.exe)?[\"']?(?:\s+-\S+)*\s+hook\s"
    r"|-m\s+aisquare\b(?:\s+-\S+)*\s+hook\s"
)
#: ``aisquare repo …`` as a COMMAND: the program word at the start of the line or
#: after a shell separator, bare or by path — not ``grep aisquare repo/``.
_LOOKUP_COMMAND = re.compile(
    r"(?:^|[;&|(`]|\$\()\s*(?:\w+=\S*\s+)*(?:\S*/)?(?:aisquare|asq)(?:\.exe)?\s+repo\b"
)

_USAGE_MARKERS = (b'"assistant"', b'"tool_result"', b'"hook_', b"aisquare", b"_delta")


def is_aisquare_hook(command: str) -> bool:
    """Whether a hook's ``command`` runs aisquare (``asq hook …``, ``-m aisquare hook …``,
    the plugin's ``aisquare-hook``) — by program, not by a path that merely names it."""
    return bool(_AISQUARE_HOOK.search(f"{command} "))


def is_lookup_call(name: str, tool_input: Any) -> bool:
    """Whether one tool call is an aisquare LOOKUP: an aisquare MCP tool, or a
    Bash command that runs ``aisquare repo …`` / ``asq repo …``."""
    if name.startswith("mcp__"):
        server = name.split("__")[1] if name.count("__") >= 2 else ""
        return "aisquare" in server.lower()
    if name == "Bash" and isinstance(tool_input, dict):
        command = tool_input.get("command")
        return isinstance(command, str) and bool(_LOOKUP_COMMAND.search(command))
    return False


@dataclass
class ModelUsage:
    """What one model was billed for across a stretch of transcript."""

    requests: int = 0
    input: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    output: int = 0
    cost_usd: float = 0.0
    """List price of these requests; 0 for an unpriced model (see ``priced``)."""
    weighted: float = 0.0
    """Cost-weighted tokens (``core.pricing``); 0 for an unpriced model."""
    priced: bool = True

    @property
    def cache_write(self) -> int:
        return self.cache_write_5m + self.cache_write_1h

    @property
    def total(self) -> int:
        return self.input + self.cache_write + self.cache_read + self.output

    def add(self, other: ModelUsage) -> None:
        self.requests += other.requests
        self.input += other.input
        self.cache_write_5m += other.cache_write_5m
        self.cache_write_1h += other.cache_write_1h
        self.cache_read += other.cache_read
        self.output += other.output
        self.cost_usd += other.cost_usd
        self.weighted += other.weighted
        self.priced = self.priced and other.priced

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "input": self.input,
            "cache_write_5m": self.cache_write_5m,
            "cache_write_1h": self.cache_write_1h,
            "cache_read": self.cache_read,
            "output": self.output,
            "cost_usd": self.cost_usd,
            "weighted": self.weighted,
            "priced": self.priced,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelUsage:
        return cls(
            requests=int(data.get("requests", 0)),
            input=int(data.get("input", 0)),
            cache_write_5m=int(data.get("cache_write_5m", 0)),
            cache_write_1h=int(data.get("cache_write_1h", 0)),
            cache_read=int(data.get("cache_read", 0)),
            output=int(data.get("output", 0)),
            cost_usd=float(data.get("cost_usd", 0.0)),
            weighted=float(data.get("weighted", 0.0)),
            priced=bool(data.get("priced", True)),
        )


@dataclass
class Usage:
    """What a stretch of transcript adds: a turn's worth, or a session's when summed."""

    models: dict[str, ModelUsage] = field(default_factory=dict)
    tools: dict[str, int] = field(default_factory=dict)
    """Tool calls by tool name."""
    tool_result_chars: int = 0
    lookup_calls: int = 0
    injected_chars: int = 0
    """Characters aisquare's hooks put in front of the model."""
    aisquare_traces: list[str] = field(default_factory=list)
    """Each sign that aisquare reached this session — empty for a native one."""

    @property
    def requests(self) -> int:
        return sum(m.requests for m in self.models.values())

    @property
    def tool_calls(self) -> int:
        return sum(self.tools.values())

    @property
    def subagent_calls(self) -> int:
        return sum(count for name, count in self.tools.items() if name in SUBAGENT_TOOLS)

    def totals(self) -> ModelUsage:
        """Every model summed into one; ``priced`` only when all of them are."""
        out = ModelUsage()
        for usage in self.models.values():
            out.add(usage)
        return out

    def is_empty(self) -> bool:
        return not (
            self.models
            or self.tools
            or self.tool_result_chars
            or self.injected_chars
            or self.aisquare_traces
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "models": {name: usage.as_dict() for name, usage in sorted(self.models.items())},
            "tools": dict(sorted(self.tools.items())),
            "tool_result_chars": self.tool_result_chars,
            "lookup_calls": self.lookup_calls,
            "injected_chars": self.injected_chars,
            "aisquare_traces": list(self.aisquare_traces),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Usage:
        models = data.get("models")
        tools = data.get("tools")
        traces = data.get("aisquare_traces")
        return cls(
            models={
                str(name): ModelUsage.from_dict(value)
                for name, value in models.items()
                if isinstance(value, dict)
            }
            if isinstance(models, dict)
            else {},
            tools={str(k): int(v) for k, v in tools.items()} if isinstance(tools, dict) else {},
            tool_result_chars=int(data.get("tool_result_chars", 0)),
            lookup_calls=int(data.get("lookup_calls", 0)),
            injected_chars=int(data.get("injected_chars", 0)),
            aisquare_traces=[str(t) for t in traces] if isinstance(traces, list) else [],
        )

    def add(self, other: Usage) -> None:
        for model, usage in other.models.items():
            self.models.setdefault(model, ModelUsage()).add(usage)
        for name, count in other.tools.items():
            self.tools[name] = self.tools.get(name, 0) + count
        self.tool_result_chars += other.tool_result_chars
        self.lookup_calls += other.lookup_calls
        self.injected_chars += other.injected_chars
        for trace in other.aisquare_traces:
            if trace not in self.aisquare_traces:
                self.aisquare_traces.append(trace)


@dataclass
class ScanState:
    """Where a session's scans have reached, and what dedupe and "first edit" remember.

    Stored with the session between Stop hooks as :meth:`to_json`, read back by :meth:`parse`.
    """

    offsets: dict[str, int] = field(default_factory=dict)
    """Byte offset consumed, per file (the transcript and each subagent file)."""
    counted: dict[str, list[int]] = field(default_factory=dict)
    """message id → the five usage fields already counted for it (newest last)."""
    tool_ids: list[str] = field(default_factory=list)
    edited: bool = False
    before_edit_tokens: int = 0
    """Raw tokens (all four kinds) of every request up to and including the one
    that made the first Edit/Write/NotebookEdit call."""
    before_edit_weighted: float = 0.0
    before_edit_tool_calls: int = 0
    """Tool calls made before that first edit call (the edit itself excluded)."""

    def first_edit(self) -> dict[str, Any]:
        """The "before the first edit" figures, as a session's usage row reports them."""
        return {
            "edited": self.edited,
            "tokens": self.before_edit_tokens,
            "weighted": self.before_edit_weighted,
            "tool_calls": self.before_edit_tool_calls,
        }

    def to_json(self) -> str:
        return json.dumps(
            {
                "offsets": self.offsets,
                "counted": self.counted,
                "tool_ids": self.tool_ids,
                "edited": self.edited,
                "before_edit_tokens": self.before_edit_tokens,
                "before_edit_weighted": self.before_edit_weighted,
                "before_edit_tool_calls": self.before_edit_tool_calls,
            },
            separators=(",", ":"),
        )

    @classmethod
    def parse(cls, raw: str | None) -> ScanState:
        """The state ``raw`` holds; a fresh one for ``None`` or anything unreadable."""
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
        except ValueError:
            return cls()
        if not isinstance(data, dict):
            return cls()
        offsets = data.get("offsets")
        counted = data.get("counted")
        tool_ids = data.get("tool_ids")
        return cls(
            offsets={str(k): int(v) for k, v in offsets.items()}
            if isinstance(offsets, dict)
            else {},
            counted={str(k): [int(x) for x in v] for k, v in counted.items()}
            if isinstance(counted, dict)
            else {},
            tool_ids=[str(x) for x in tool_ids] if isinstance(tool_ids, list) else [],
            edited=bool(data.get("edited", False)),
            before_edit_tokens=int(data.get("before_edit_tokens", 0)),
            before_edit_weighted=float(data.get("before_edit_weighted", 0.0)),
            before_edit_tool_calls=int(data.get("before_edit_tool_calls", 0)),
        )


def transcript_files(transcript: Path) -> list[Path]:
    """The session's transcript and its subagents' files, transcript first."""
    files = [transcript]
    subagents = transcript.with_suffix("") / "subagents"
    with contextlib.suppress(OSError):
        files.extend(sorted(subagents.glob("*.jsonl")))
    return files


def scan(transcript: Path, state: ScanState | None = None) -> tuple[Usage, ScanState]:
    """What the transcript (and its subagents' files) added since ``state``.

    Returns the delta and the state to pass next time; ``state`` itself is
    updated in place and returned. A fresh state reads every file from the start.
    """
    state = state if state is not None else ScanState()
    stamped: list[tuple[str, int, int, dict[str, Any]]] = []
    for order, path in enumerate(transcript_files(transcript)):
        key = str(path)
        start = state.offsets.get(key, 0)
        raw, consumed = _read_from(path, start)
        if consumed:
            state.offsets[key] = start + consumed
        for line_no, entry in enumerate(_entries(raw)):
            stamp = entry.get("timestamp")
            stamped.append((stamp if isinstance(stamp, str) else "", order, line_no, entry))
    # Timestamps are ISO-8601 UTC from one clock, so they sort as strings. An entry
    # without one sorts first within its file, where it was found.
    stamped.sort(key=lambda item: (item[0], item[1], item[2]))
    usage = Usage()
    for _, _, _, entry in stamped:
        _account(entry, usage, state)
    _trim(state)
    return usage, state


def _read_from(path: Path, start: int) -> tuple[list[bytes], int]:
    """The complete lines of ``path`` from byte ``start``, and how many bytes they span.

    A file that shrank below ``start`` (rewritten, not appended to) is read again
    from the beginning; the dedupe by message id keeps a request counted once.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            end = handle.tell()
            if end < start:
                start = 0
            handle.seek(start)
            data = handle.read(end - start)
    except OSError:
        return [], 0
    cut = data.rfind(b"\n")
    if cut < 0:
        return [], 0  # no complete line yet: a reply still being written
    complete = data[: cut + 1]
    return complete.split(b"\n"), len(complete)


def _entries(lines: Iterable[bytes]) -> Iterator[dict[str, Any]]:
    for line in lines:
        # The bytes test first: most lines (api-request blobs, titles, modes) carry
        # nothing this reader counts, and json.loads is where the time goes.
        if not line or line.startswith(b'{"type":"api-request'):
            continue
        if not any(marker in line for marker in _USAGE_MARKERS):
            continue
        try:
            entry = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(entry, dict):
            yield entry


def _account(entry: dict[str, Any], usage: Usage, state: ScanState) -> None:
    kind = entry.get("type")
    if kind == "assistant":
        _assistant(entry, usage, state)
    elif kind == "user":
        _user(entry, usage)
    elif kind == "attachment":
        _attachment(entry, usage)


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _fields(raw: dict[str, Any]) -> list[int]:
    """``[input, cache_write_5m, cache_write_1h, cache_read, output]`` of one usage block.

    Cache writes with no TTL breakdown are counted as 5-minute ones (the API's
    default TTL, and the cheaper price): the breakdown is what says otherwise.
    """
    written = _int(raw.get("cache_creation_input_tokens"))
    split = raw.get("cache_creation")
    one_hour = _int(split.get("ephemeral_1h_input_tokens")) if isinstance(split, dict) else 0
    one_hour = min(one_hour, written)
    return [
        _int(raw.get("input_tokens")),
        written - one_hour,
        one_hour,
        _int(raw.get("cache_read_input_tokens")),
        _int(raw.get("output_tokens")),
    ]


def _assistant(entry: dict[str, Any], usage: Usage, state: ScanState) -> None:
    message = entry.get("message")
    if not isinstance(message, dict) or entry.get("isApiErrorMessage") is True:
        return
    model = message.get("model")
    if not isinstance(model, str) or not model or model == "<synthetic>":
        return
    raw_usage = message.get("usage")
    if isinstance(raw_usage, dict):
        _count_request(model, message.get("id"), _fields(raw_usage), usage, state)
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            _tool_use(block, usage, state)


def _count_request(
    model: str, message_id: Any, fields: list[int], usage: Usage, state: ScanState
) -> None:
    key = message_id if isinstance(message_id, str) and message_id else None
    seen = state.counted.get(key) if key is not None else None
    if seen is None:
        delta = fields
        new_request = True
    else:
        delta = [max(0, now - before) for now, before in zip(fields, seen, strict=True)]
        new_request = False
    if key is not None:
        state.counted.pop(key, None)  # re-inserted last: the dict is the LRU order
        state.counted[key] = [max(a, b) for a, b in zip(fields, seen or fields, strict=True)]
    if not new_request and not any(delta):
        return
    tokens = RequestTokens(*delta)
    bucket = usage.models.setdefault(pricing.canonical_model(model), ModelUsage())
    bucket.requests += 1 if new_request else 0
    bucket.input += tokens.input
    bucket.cache_write_5m += tokens.cache_write_5m
    bucket.cache_write_1h += tokens.cache_write_1h
    bucket.cache_read += tokens.cache_read
    bucket.output += tokens.output
    cost = pricing.cost_usd(model, tokens)
    weighted = pricing.weighted_tokens(model, tokens)
    if cost is None or weighted is None:
        bucket.priced = False
    else:
        bucket.cost_usd += cost
        bucket.weighted += weighted
    if not state.edited:
        state.before_edit_tokens += tokens.total
        state.before_edit_weighted += weighted or 0.0


def _tool_use(block: dict[str, Any], usage: Usage, state: ScanState) -> None:
    tool_id = block.get("id")
    if isinstance(tool_id, str) and tool_id:
        if tool_id in state.tool_ids:
            return
        state.tool_ids.append(tool_id)
    name = block.get("name")
    if not isinstance(name, str) or not name:
        return
    usage.tools[name] = usage.tools.get(name, 0) + 1
    if is_lookup_call(name, block.get("input")):
        usage.lookup_calls += 1
    if name.startswith("mcp__") and is_lookup_call(name, None):
        _trace(usage, f"aisquare MCP tool called: {name}")
    if state.edited:
        return
    if name in EDIT_TOOLS:
        state.edited = True
    else:
        state.before_edit_tool_calls += 1


def _user(entry: dict[str, Any], usage: Usage) -> None:
    message = entry.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if isinstance(content, str):
        _scan_for_markers(content, "a user message", usage)
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            usage.tool_result_chars += len(_result_text(block.get("content")))
        elif block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                _scan_for_markers(text, "a user message", usage)


def _result_text(content: Any) -> str:
    """A tool result's text: a string, or the text parts of a list (images count 0)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _attachment(entry: dict[str, Any], usage: Usage) -> None:
    attachment = entry.get("attachment")
    if not isinstance(attachment, dict):
        return
    kind = str(attachment.get("type") or "")
    if kind.startswith("hook"):
        command = attachment.get("command")
        event = str(attachment.get("hookEvent") or attachment.get("hookName") or "?")
        if isinstance(command, str) and is_aisquare_hook(command):
            _trace(usage, f"aisquare hook ran: {event}")
            if kind == "hook_success":
                usage.injected_chars += _injected(event, attachment)
        content = attachment.get("content")
        if isinstance(content, str):
            _scan_for_markers(content, f"{event} hook output", usage)
        return
    if kind in ("deferred_tools_delta", "mcp_instructions_delta"):
        names = attachment.get("addedNames")
        if isinstance(names, list):
            for name in names:
                text = str(name)
                if (text.startswith("mcp__") and is_lookup_call(text, None)) or (
                    kind == "mcp_instructions_delta" and "aisquare" in text.lower()
                ):
                    _trace(usage, f"aisquare MCP offered: {text}")


def _injected(event: str, attachment: dict[str, Any]) -> int:
    """How many characters one aisquare hook put in front of the model.

    ``content`` for the hooks whose stdout is context; for ``Stop``, the
    ``reason`` of a ``block`` decision, which Claude Code feeds back as the next
    turn's prompt; nothing for the bookkeeping hooks.
    """
    content = attachment.get("content")
    if not isinstance(content, str) or not content:
        return 0
    if event.split(":", 1)[0] in _CONTEXT_EVENTS:
        return len(content)
    if event.startswith("Stop") and not event.startswith("StopFailure"):
        try:
            decision = json.loads(content)
        except ValueError:
            return 0
        if isinstance(decision, dict) and decision.get("decision") == "block":
            reason = decision.get("reason")
            return len(reason) if isinstance(reason, str) else 0
    return 0


def _scan_for_markers(text: str, where: str, usage: Usage) -> None:
    for marker in AISQUARE_MARKERS:
        if marker in text:
            _trace(usage, f"aisquare context in {where}")
            return


def _trace(usage: Usage, trace: str) -> None:
    if trace not in usage.aisquare_traces:
        usage.aisquare_traces.append(trace)


def _trim(state: ScanState) -> None:
    if len(state.counted) > _RECENT_IDS:
        for key in list(state.counted)[: len(state.counted) - _RECENT_IDS]:
            del state.counted[key]
    if len(state.tool_ids) > _RECENT_IDS:
        del state.tool_ids[: len(state.tool_ids) - _RECENT_IDS]
