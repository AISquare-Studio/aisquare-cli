"""What a transcript reader keeps between scans of a session's token usage, for an agent whose
records it reads one line at a time (``AgentTranscript.usage``): Codex's rollout, and any
agent after it. Claude Code's reader is main's own (``claude_usage``).

A scan reads only the complete lines appended since the last (:meth:`Tally.read`), counts each
model request once (:meth:`Tally.request`: a record seen again adds only what grew), prices it
at the agent's own table (``AgentAdapter.price_table``), counts the tool calls
(:meth:`Tally.tool`) and the tokens spent before the first edit, and hands back the delta with
the state to pass next time (:meth:`Tally.scan`). A file that shrank was rewritten: it is read
again from its start, and only what is newer than the newest record read before counts, as
``core.usage.scan`` reads one. Nothing here raises on what a file holds, and nothing reads a
whole file twice.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aisquare.core.agent_adapters.types import FirstEdit, PriceTable, UsageScan
from aisquare.core.pricing import RequestTokens
from aisquare.core.usage import AISQUARE_MARKERS, ModelUsage, Usage

#: How many request keys, tool-call ids and turns a session remembers between scans: a
#: record has only to outlast a scan falling between two of its lines.
RECENT = 512

#: ``aisquare repo …`` as a COMMAND: the program word at the start of the line or after a
#: shell separator, bare or by path (``core.usage``'s rule, for any agent's shell tool).
_LOOKUP_COMMAND = re.compile(
    r"(?:^|[;&|(`]|\$\()\s*(?:\w+=\S*\s+)*(?:\S*/)?(?:aisquare|asq)(?:\.exe)?\s+repo\b"
)


def is_lookup_command(command: str) -> bool:
    """Whether a shell command runs an aisquare LOOKUP (``aisquare repo …``, ``asq repo …``)."""
    return bool(_LOOKUP_COMMAND.search(command))


def is_lookup_server(server: str) -> bool:
    """Whether an MCP server, by the name a session calls it, is aisquare's."""
    return "aisquare" in server.lower()


def appended(path: Path, start: int) -> tuple[list[bytes], int, bool]:
    """The complete lines of ``path`` from byte ``start``, how many bytes they span, and
    whether it was read from its beginning instead, having shrunk below ``start`` (rewritten,
    not appended to). A last line without its newline is left for the next read: a record
    still being written. Nothing for a file that cannot be read."""
    restarted = False
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            end = handle.tell()
            if end < start:
                start, restarted = 0, True
            handle.seek(start)
            data = handle.read(end - start)
    except OSError:
        return [], 0, False
    cut = data.rfind(b"\n")
    if cut < 0:
        return [], 0, restarted
    complete = data[: cut + 1]
    return complete.split(b"\n"), len(complete), restarted


def _fields(raw: Any) -> list[int] | None:
    """A request's five counted fields as stored, or ``None`` for anything else."""
    if not isinstance(raw, list) or len(raw) != 5:
        return None
    return [int(x) for x in raw]


@dataclass
class Tally:
    """Where a session's scans have reached, and what dedupe and "before the first edit"
    remember: the text :meth:`to_json` writes is the ``state`` a reader hands back."""

    offsets: dict[str, int] = field(default_factory=dict)
    """Bytes consumed, per file."""
    newest: dict[str, str] = field(default_factory=dict)
    """The newest record's ``timestamp`` read, per file: a file read again from its start
    counts only what is newer."""
    counted: dict[str, list[int]] = field(default_factory=dict)
    """Request key → its ``[input, cache_write_5m, cache_write_1h, cache_read, output]`` so
    far, oldest first (an LRU of :data:`RECENT`)."""
    tool_ids: list[str] = field(default_factory=list)
    turns: dict[str, str] = field(default_factory=dict)
    """A reader's own join, oldest first: Codex's turn id → the model its ``turn_context``
    names."""
    edited: bool = False
    editing: bool = False
    """The first edit is called, and the request that called it is still to be counted: an
    agent that writes a response's tokens after its calls (Codex)."""
    before_edit_tokens: int = 0
    before_edit_weighted: float = 0.0
    before_edit_tool_calls: int = 0

    @classmethod
    def parse(cls, raw: str | None) -> Tally:
        """The tally ``raw`` holds; a fresh one for ``None`` or anything unreadable."""
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return cls()
        try:
            return cls(
                offsets={str(k): int(v) for k, v in dict(data.get("offsets") or {}).items()},
                newest={str(k): str(v) for k, v in dict(data.get("newest") or {}).items()},
                counted={
                    str(k): kept
                    for k, v in dict(data.get("counted") or {}).items()
                    if (kept := _fields(v)) is not None
                },
                tool_ids=[str(x) for x in data.get("tool_ids") or []],
                turns={str(k): str(v) for k, v in dict(data.get("turns") or {}).items()},
                edited=bool(data.get("edited", False)),
                editing=bool(data.get("editing", False)),
                before_edit_tokens=int(data.get("before_edit_tokens", 0)),
                before_edit_weighted=float(data.get("before_edit_weighted", 0.0)),
                before_edit_tool_calls=int(data.get("before_edit_tool_calls", 0)),
            )
        except (TypeError, ValueError):
            return cls()

    def to_json(self) -> str:
        for remembered in (self.counted, self.turns):
            for key in list(remembered)[: max(0, len(remembered) - RECENT)]:
                del remembered[key]
        del self.tool_ids[: max(0, len(self.tool_ids) - RECENT)]
        return json.dumps(
            {
                "offsets": self.offsets,
                "newest": self.newest,
                "counted": self.counted,
                "tool_ids": self.tool_ids,
                "turns": self.turns,
                "edited": self.edited,
                "editing": self.editing,
                "before_edit_tokens": self.before_edit_tokens,
                "before_edit_weighted": self.before_edit_weighted,
                "before_edit_tool_calls": self.before_edit_tool_calls,
            },
            separators=(",", ":"),
        )

    def read(self, path: Path, wanted: tuple[bytes, ...] = ()) -> Iterator[dict[str, Any]]:
        """The records appended to ``path`` since the last scan, in the file's order, each a
        JSON object a line; ``wanted``, where given, the bytes one of which a line must hold
        to be parsed at all (most of a transcript is records no reader counts). Read again
        from its start, a rewritten file yields only what is newer than its newest before."""
        key = str(path)
        start = self.offsets.get(key, 0)
        lines, consumed, restarted = appended(path, start)
        if consumed:
            self.offsets[key] = (0 if restarted else start) + consumed
        newest = self.newest.get(key, "")
        for line in lines:
            if not line or (wanted and not any(marker in line for marker in wanted)):
                continue
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError, RecursionError):
                continue
            if not isinstance(record, dict):
                continue
            stamp = record.get("timestamp")
            stamp = stamp if isinstance(stamp, str) else ""
            if restarted and stamp <= newest:
                continue  # read before the file was rewritten: counted then
            newest = max(newest, stamp)
            yield record
        if newest:
            self.newest[key] = newest

    def request(
        self,
        usage: Usage,
        table: PriceTable | None,
        model: str,
        key: str | None,
        tokens: RequestTokens,
    ) -> None:
        """Count one model request of ``model``, keyed ``key`` (``None``: counted as new):
        what a record seen again adds over what was counted for it, priced at the request's
        own tier off ``table`` (unpriced without one, or for a model it lacks)."""
        fields = [
            tokens.input,
            tokens.cache_write_5m,
            tokens.cache_write_1h,
            tokens.cache_read,
            tokens.output,
        ]
        seen = self.counted.pop(key, None) if key is not None else None
        delta = (
            fields if seen is None else [max(0, a - b) for a, b in zip(fields, seen, strict=True)]
        )
        whole = fields if seen is None else [max(a, b) for a, b in zip(fields, seen, strict=True)]
        if key is not None:
            self.counted[key] = whole  # re-inserted last: the dict is the LRU order
        if seen is not None and not any(delta):
            return
        added = RequestTokens(*delta)
        prompt = RequestTokens(*whole).prompt
        name = table.canonical(model) if table is not None else model.strip()
        bucket = usage.models.setdefault(name, ModelUsage())
        bucket.requests += 1 if seen is None else 0
        bucket.input += added.input
        bucket.cache_write_5m += added.cache_write_5m
        bucket.cache_write_1h += added.cache_write_1h
        bucket.cache_read += added.cache_read
        bucket.output += added.output
        cost = table.cost_usd(model, added, prompt=prompt) if table is not None else None
        weighted = table.weighted_tokens(model, added, prompt=prompt) if table is not None else None
        if cost is None or weighted is None:
            bucket.priced = False
        else:
            bucket.cost_usd += cost
            bucket.weighted += weighted
        if not self.edited:
            self.before_edit_tokens += added.total
            self.before_edit_weighted += weighted or 0.0
            if self.editing:
                self.edited, self.editing = True, False

    def tool(
        self,
        usage: Usage,
        name: str,
        call_id: str | None,
        *,
        edits: bool,
        lookup: bool = False,
        billed_after: bool = False,
    ) -> None:
        """Count one tool call (``call_id`` once): by ``name``, as a lookup where it is one, and
        before the first edit unless it ``edits``. ``billed_after``: the agent writes the
        request that made the call after it, which then still counts before the first edit."""
        if call_id:
            if call_id in self.tool_ids:
                return
            self.tool_ids.append(call_id)
        usage.tools[name] = usage.tools.get(name, 0) + 1
        usage.lookup_calls += 1 if lookup else 0
        if self.edited or self.editing:
            return
        if not edits:
            self.before_edit_tool_calls += 1
        elif billed_after:
            self.editing = True
        else:
            self.edited = True

    def first_edit(self) -> FirstEdit:
        return FirstEdit(
            self.edited,
            self.before_edit_tokens,
            self.before_edit_weighted,
            self.before_edit_tool_calls,
        )

    def scan(self, usage: Usage) -> UsageScan:
        """``usage``, what this scan read, with the state to pass to the next."""
        return UsageScan(usage, self.to_json(), self.first_edit())


def trace(usage: Usage, text: str) -> None:
    """Record one sign that aisquare reached the session, once."""
    if text not in usage.aisquare_traces:
        usage.aisquare_traces.append(text)


def carries_context(text: str) -> bool:
    """Whether ``text`` holds aisquare's own context, framed in its markers."""
    return any(marker in text for marker in AISQUARE_MARKERS)
