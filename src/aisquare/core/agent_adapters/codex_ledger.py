"""Codex's answer to ``AgentTranscript.ledger``, ``context_tokens`` and ``compacted_since``:
the token ledger (#296's WS-0) read off its session rollout.

Where :mod:`aisquare.core.ledger`'s terms sit in a Codex rollout (measured on 0.164.0 and
0.162.1 with a loopback provider; ``codex_usage`` has the record shapes):

- **request** — a ``token_usage_record``, keyed by its ``response_id`` and the thread's
  running total, so one read twice counts once. Its ``usage.input_tokens`` HOLDS the cached
  ones, so fresh is ``input_tokens - cached_input_tokens - cache_write_input_tokens``,
  read is ``cached_input_tokens`` and written ``cache_write_input_tokens``; the context is
  ``input_tokens``.
- **baseline** — the first record's ``input_tokens``.
- **tool call** — a ``response_item`` call (``function_call``, ``custom_tool_call``,
  ``local_shell_call``) by name, an MCP tool's as ``<namespace>__<name>``; a board read is
  a shell call (``exec_command``'s ``cmd``, ``shell``'s ``command``) that only reads.
- **wake** — the Stop hook's continuation, which Codex runs as a ``user`` message
  (``capabilities.stop_reason_as_prompt``) holding the marker; its window closes at the
  turn's ``task_complete``, or at a prompt.
- **delta** — the blocks aisquare's hooks hand Codex, which it keeps as ``developer`` (and
  ``user``) messages.
- **compaction** — a ``compacted`` record, which Codex writes for ``/compact`` and its own
  automatic compaction alike, so its trigger is ``unknown``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from aisquare.core import ledger
from aisquare.core.agent_adapters.codex_transcript import (
    _call_input,
    _call_name,
    _message_text,
    _own,
)
from aisquare.core.ledger import SessionLedger

#: The calls a response makes, each a tool run (``codex_usage``'s).
_CALLS = frozenset({"function_call", "custom_tool_call", "local_shell_call"})
#: The namespace Codex writes on its own tools' calls: no MCP server's.
_OWN_NAMESPACE = "functions"
#: Its shell tools, whose command a board read is read off.
_SHELLS = frozenset({"exec_command", "shell", "local_shell"})


def read(file: Path, *, since: datetime | None, until: datetime | None) -> SessionLedger:
    """The counts of the rollout ``file``, from the records stamped in ``[since, until]``."""
    row = SessionLedger(session_id="", role="", transcript_found=True)
    window = ledger.Window(row)
    seen: set[str] = set()
    for record in ledger.entries(file, row):
        kind = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        request = _request(payload) if kind == "token_usage_record" else None
        if request is not None and row.baseline is None:
            row.baseline = sum(request[:3])  # the session's first, whatever the window
        if not ledger.within(record, since, until):
            continue
        if request is not None:
            key = _key(payload)
            if key is not None and key in seen:
                continue
            if key is not None:
                seen.add(key)
            window.request(*request)
        elif kind == "compacted":
            window.compacted("unknown")
        elif kind == "event_msg" and payload.get("type") == "task_complete":
            window.turn_ended()
        elif kind == "response_item":
            _item(payload, window)
    return window.finish()


def context_tokens(file: Path) -> int | None:
    """The context now: the newest ``token_usage_record``'s ``input_tokens``."""
    row = SessionLedger(session_id="", role="")
    latest: int | None = None
    for record in ledger.entries(file, row):
        payload = record.get("payload")
        if record.get("type") == "token_usage_record" and isinstance(payload, dict):
            request = _request(payload)
            if request is not None:
                latest = sum(request[:3])
    return latest


def compacted_since(file: Path, since: datetime) -> bool:
    """Whether a ``compacted`` record was written at or after ``since``: what a typed
    ``/compact`` leaves, with no hook (measured on 0.164.0)."""
    row = SessionLedger(session_id="", role="")
    for record in ledger.entries(file, row):
        if record.get("type") == "compacted":
            stamp = ledger.stamped(record)
            if stamp is not None and stamp >= since:
                return True
    return False


def _request(payload: dict[str, Any]) -> tuple[int, int, int, int] | None:
    """``(fresh, written, read, output)`` of a ``token_usage_record``, or ``None``."""
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    read = ledger.tokens(usage.get("cached_input_tokens"))
    written = ledger.tokens(usage.get("cache_write_input_tokens"))
    fresh = max(0, ledger.tokens(usage.get("input_tokens")) - read - written)
    return fresh, written, read, ledger.tokens(usage.get("output_tokens"))


def _key(payload: dict[str, Any]) -> str | None:
    response = payload.get("response_id")
    thread = payload.get("thread_token_usage")
    running = ledger.tokens(thread.get("total_tokens")) if isinstance(thread, dict) else 0
    return f"{response}:{running}" if isinstance(response, str) and response else None


def _item(payload: dict[str, Any], window: ledger.Window) -> None:
    kind = payload.get("type")
    if kind in _CALLS:
        name = _call_name(payload)
        namespace = payload.get("namespace")
        shell = name in _SHELLS
        if isinstance(namespace, str) and namespace and namespace != _OWN_NAMESPACE:
            name = f"{namespace}__{name}"
        arguments = _call_input(payload)
        command = (
            arguments.get("cmd", arguments.get("command")) if isinstance(arguments, dict) else None
        )
        board_read = shell and isinstance(command, str) and ledger.is_board_read(command)
        window.tool(name, board_read=board_read)
    elif kind == "message":
        text = _message_text(payload)
        role = payload.get("role")
        if role == "user" and ledger.WAKE_MARKER in text:
            window.wake()
        elif role == "user" and text.strip() and not _own(text):
            window.turn_ended()  # someone typed
        if role in ("developer", "user"):
            window.deltas(text)
