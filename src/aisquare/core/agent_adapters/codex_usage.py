"""Codex's token usage, read off its session rollout INCREMENTALLY: its answer to
``AgentTranscript.usage`` (:func:`usage`), kept between scans in a ``usage_tally.Tally``.

Measured on 0.164.0 and 0.162.1 with a loopback provider (SCOPE #113 main-274, N1 and N2; the
rollouts in ``tests/fixtures/agent_transcripts/codex-usage-*.jsonl``):

- **Every model response** leaves one ``token_usage_record`` line, ``{thread_id, turn_id,
  response_id, usage, turn_token_usage, thread_token_usage}``, each usage ``{input_tokens,
  cached_input_tokens, cache_write_input_tokens, output_tokens, reasoning_output_tokens,
  total_tokens}``. ``input_tokens`` holds the cached ones (1000 with 600 cached is 400 billed
  as input), and ``output_tokens`` the reasoning. ``cache_write_input_tokens`` is read, by
  its name, as a part of ``input_tokens`` as the cached ones are: 0 in every record measured.
- **The model** of a response is in its turn's ``turn_context`` alone (``{turn_id, model}``),
  which comes before the turn's records: a record takes its ``turn_id``'s, else the newest.
- **What is not counted:** ``event_msg`` ``token_count`` repeats the totals (a turn refused
  with a 429 writes one with the last totals again and no record), and a failed turn writes
  no record at all. A record is keyed by its ``response_id`` and the thread's running total
  it brings, so one read twice counts once, and a provider that reuses its ids (a loopback
  one's ``resp_local``) has each of its responses counted.
- **Tool calls** are the ``response_item`` calls (``function_call``, ``custom_tool_call``,
  ``local_shell_call``) by name, an MCP tool's as ``<namespace>__<name>``; ``apply_patch``
  is the first edit, and its response, whose record follows it, counts before it. An
  aisquare MCP tool, or a shell command that runs ``aisquare repo``, is a lookup.
- **Injected characters** are the ``developer`` messages holding aisquare's context
  markers: Codex hands a ``SessionStart`` hook's output to the model as one (N4), and the
  thread keeps it, so it rides every later request too.

Unmeasured, and so not read: the rollouts of a ``multi_agent`` sub-thread, which Codex writes
apart. A turn interrupted mid-response is counted at the next scan, when its record is
written.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aisquare.core.agent_adapters.codex import PRICE_TABLE
from aisquare.core.agent_adapters.codex_transcript import (
    _call_id,
    _call_input,
    _call_name,
    _message_text,
)
from aisquare.core.agent_adapters.types import UsageScan
from aisquare.core.agent_adapters.usage_tally import (
    Tally,
    carries_context,
    is_lookup_command,
    is_lookup_server,
    trace,
)
from aisquare.core.pricing import RequestTokens
from aisquare.core.usage import Usage

#: The tool whose first call ends a session's exploration: Codex edits files with it.
EDIT_TOOL = "apply_patch"
#: The calls a response makes, each a tool run (``codex_transcript``'s).
_CALLS = frozenset({"function_call", "custom_tool_call", "local_shell_call"})
_RESULTS = frozenset({"function_call_output", "custom_tool_call_output"})
#: The namespace Codex 0.162.1 writes on its own tools' calls: no MCP server's.
_OWN_NAMESPACE = "functions"
#: A line this reader may count carries one of these.
_WANTED = (
    b'"token_usage_record"',
    b'"turn_context"',
    b"_call",
    b"<aisquare-",
)
#: The model a record whose turn named none is counted under: unpriced.
UNKNOWN_MODEL = "unknown"


def usage(file: Path, state: str | None) -> UsageScan:
    """What the rollout ``file`` added since ``state`` (module docstring)."""
    tally = Tally.parse(state)
    found = Usage()
    for record in tally.read(file, _WANTED):
        kind = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if kind == "turn_context":
            _turn(payload, tally)
        elif kind == "token_usage_record":
            _response(payload, tally, found)
        elif kind == "response_item":
            _item(payload, tally, found)
    return tally.scan(found)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _turn(payload: Mapping[str, Any], tally: Tally) -> None:
    turn, model = _text(payload.get("turn_id")), _text(payload.get("model"))
    if turn is not None and model is not None:
        tally.turns.pop(turn, None)
        tally.turns[turn] = model  # last: the newest turn's model is the fallback


def _response(payload: Mapping[str, Any], tally: Tally, found: Usage) -> None:
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        return
    cached = _count(raw.get("cached_input_tokens"))
    written = _count(raw.get("cache_write_input_tokens"))
    tokens = RequestTokens(
        input=max(0, _count(raw.get("input_tokens")) - cached - written),
        cache_write_5m=written,
        cache_read=cached,
        output=_count(raw.get("output_tokens")),
    )
    turn = _text(payload.get("turn_id"))
    model = (tally.turns.get(turn) if turn is not None else None) or next(
        reversed(tally.turns.values()), UNKNOWN_MODEL
    )
    response = _text(payload.get("response_id"))
    thread = payload.get("thread_token_usage")
    running = _count(thread.get("total_tokens")) if isinstance(thread, dict) else 0
    key = f"{response}:{running}" if response is not None else None
    tally.request(found, PRICE_TABLE, model, key, tokens)


def _item(payload: Mapping[str, Any], tally: Tally, found: Usage) -> None:
    kind = payload.get("type")
    if kind in _CALLS:
        _call(payload, tally, found)
    elif kind in _RESULTS:
        output = payload.get("output")
        found.tool_result_chars += len(_message_text({"content": output}) if output else "")
    elif kind == "message":
        text = _message_text(payload)
        if not carries_context(text):
            return
        if payload.get("role") == "developer":
            found.injected_chars += len(text)
            trace(found, "aisquare context in a developer message")
        else:
            trace(found, f"aisquare context in a {payload.get('role') or 'user'} message")


def _call(payload: Mapping[str, Any], tally: Tally, found: Usage) -> None:
    name = _call_name(payload)
    namespace = _text(payload.get("namespace"))
    if namespace is not None and namespace != _OWN_NAMESPACE:
        name = f"{namespace}__{name}"
    server = namespace.removeprefix("mcp__") if namespace and namespace.startswith("mcp__") else ""
    arguments = _call_input(payload)
    command = (
        arguments.get("cmd", arguments.get("command")) if isinstance(arguments, dict) else None
    )
    lookup = (bool(server) and is_lookup_server(server)) or (
        isinstance(command, str) and is_lookup_command(command)
    )
    if server and is_lookup_server(server):
        trace(found, f"aisquare MCP tool called: {name}")
    tally.tool(
        found,
        name,
        _call_id(payload),
        edits=name == EDIT_TOOL,
        lookup=lookup,
        billed_after=True,
    )
