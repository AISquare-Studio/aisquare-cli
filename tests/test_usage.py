"""Token usage read off a transcript: once per request, incrementally, and honestly.

The shapes are the ones Claude Code 2.1.309 writes (``core.usage``'s docstring):
a streamed reply is several lines sharing one ``message.id`` and one usage
block, so the count that matters most here is that it is counted ONCE.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import usage
from tests.budgets import budget

MODEL = "claude-sonnet-5-5"


def _assistant(
    message_id: str,
    *blocks: dict[str, Any],
    model: str = MODEL,
    input_tokens: int = 2,
    cache_write: int = 0,
    cache_write_1h: int = 0,
    cache_read: int = 0,
    output: int = 10,
    stamp: str = "2026-10-23T10:00:00.000Z",
) -> dict[str, Any]:
    return {
        "type": "assistant",
        "timestamp": stamp,
        "message": {
            "id": message_id,
            "model": model,
            "content": list(blocks) or [{"type": "text", "text": "hi"}],
            "usage": {
                "input_tokens": input_tokens,
                "cache_creation_input_tokens": cache_write,
                "cache_read_input_tokens": cache_read,
                "output_tokens": output,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": cache_write - cache_write_1h,
                    "ephemeral_1h_input_tokens": cache_write_1h,
                },
            },
        },
    }


def _tool(tool_id: str, name: str, **tool_input: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def _result(tool_id: str, text: str, stamp: str = "2026-10-23T10:00:01.000Z") -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": stamp,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": text}],
        },
    }


def _hook(event: str, command: str, content: str) -> dict[str, Any]:
    return {
        "type": "attachment",
        "timestamp": "2026-10-23T09:59:59.000Z",
        "attachment": {
            "type": "hook_success",
            "hookName": event,
            "hookEvent": event,
            "command": command,
            "content": content,
        },
    }


def _write(path: Path, *entries: dict[str, Any], mode: str = "w") -> None:
    with path.open(mode, encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


# --- one request, counted once ----------------------------------------------------


def test_a_streamed_reply_is_counted_once_per_message_id(tmp_path: Path) -> None:
    transcript = tmp_path / "s.jsonl"
    thinking = {"type": "thinking", "thinking": ""}
    _write(
        transcript,
        _assistant("msg_1", thinking, cache_write=100, cache_read=1000, output=50),
        _assistant(
            "msg_1", _tool("t1", "Read", file_path="a"), cache_write=100, cache_read=1000, output=50
        ),
        _assistant(
            "msg_1", _tool("t2", "Grep", pattern="x"), cache_write=100, cache_read=1000, output=50
        ),
    )
    found, _ = usage.scan(transcript)
    model = found.models[MODEL]
    assert model.requests == 1
    assert (model.input, model.cache_write, model.cache_read, model.output) == (2, 100, 1000, 50)
    assert found.tools == {"Read": 1, "Grep": 1}


def test_a_later_line_with_a_larger_figure_adds_only_the_increase(tmp_path: Path) -> None:
    """An earlier build wrote ``output_tokens`` as it grew: count the final figure, once."""
    transcript = tmp_path / "s.jsonl"
    _write(transcript, _assistant("msg_1", output=5), _assistant("msg_1", output=42))
    found, _ = usage.scan(transcript)
    assert found.models[MODEL].output == 42
    assert found.models[MODEL].requests == 1


def test_a_request_split_across_two_scans_is_still_counted_once(tmp_path: Path) -> None:
    transcript = tmp_path / "s.jsonl"
    _write(transcript, _assistant("msg_1", output=30))
    first, state = usage.scan(transcript)
    _write(transcript, _assistant("msg_1", output=30), _assistant("msg_2", output=7), mode="a")
    second, state = usage.scan(transcript, state)
    assert first.models[MODEL].output == 30
    assert second.models[MODEL].output == 7
    assert second.models[MODEL].requests == 1


def test_synthetic_api_error_entries_are_not_requests(tmp_path: Path) -> None:
    transcript = tmp_path / "s.jsonl"
    synthetic = _assistant("msg_x", model="<synthetic>", output=0)
    synthetic["isApiErrorMessage"] = True
    _write(transcript, synthetic, _assistant("msg_1"))
    found, _ = usage.scan(transcript)
    assert list(found.models) == [MODEL]
    assert found.requests == 1


def test_cache_writes_are_split_by_ttl_and_unlabelled_ones_are_five_minute(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "s.jsonl"
    unlabelled = _assistant("msg_2", cache_write=300)
    del unlabelled["message"]["usage"]["cache_creation"]
    _write(transcript, _assistant("msg_1", cache_write=1000, cache_write_1h=600), unlabelled)
    model = usage.scan(transcript)[0].models[MODEL]
    assert model.cache_write_1h == 600
    assert model.cache_write_5m == 400 + 300


# --- incremental ----------------------------------------------------------------


def test_a_scan_reads_only_what_was_appended_since_the_last(tmp_path: Path) -> None:
    transcript = tmp_path / "s.jsonl"
    _write(transcript, _assistant("msg_1", output=10))
    first, state = usage.scan(transcript)
    again, state = usage.scan(transcript, state)
    assert again.is_empty()
    _write(transcript, _assistant("msg_2", output=20), mode="a")
    third, _ = usage.scan(transcript, state)
    assert first.models[MODEL].output == 10
    assert third.models[MODEL].output == 20


def test_a_line_still_being_written_is_left_for_the_next_scan(tmp_path: Path) -> None:
    transcript = tmp_path / "s.jsonl"
    whole = json.dumps(_assistant("msg_1", output=10)) + "\n"
    partial = json.dumps(_assistant("msg_2", output=99))
    transcript.write_text(whole + partial[:40], encoding="utf-8")
    first, state = usage.scan(transcript)
    assert first.models[MODEL].output == 10
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(partial[40:] + "\n")
    second, _ = usage.scan(transcript, state)
    assert second.models[MODEL].output == 99


def test_the_state_survives_a_round_trip_through_json(tmp_path: Path) -> None:
    transcript = tmp_path / "s.jsonl"
    _write(transcript, _assistant("msg_1", _tool("t1", "Edit", file_path="a"), output=10))
    _, state = usage.scan(transcript)
    restored = usage.ScanState.parse(state.to_json())
    assert restored == state
    assert usage.ScanState.parse("not json") == usage.ScanState()
    assert usage.ScanState.parse(None) == usage.ScanState()


def test_a_missing_or_garbled_transcript_reads_as_nothing(tmp_path: Path) -> None:
    found, _ = usage.scan(tmp_path / "absent.jsonl")
    assert found.is_empty()
    garbled = tmp_path / "g.jsonl"
    garbled.write_text('{"type": "assistant", "message": \nnot json at all\n[1, 2]\n')
    assert usage.scan(garbled)[0].is_empty()


def test_subagent_files_count_and_merge_in_time_order(tmp_path: Path) -> None:
    transcript = tmp_path / "sess.jsonl"
    _write(
        transcript,
        _assistant("msg_1", _tool("t1", "Task"), output=5, stamp="2026-10-23T10:00:00Z"),
        _assistant("msg_3", _tool("t3", "Edit"), output=5, stamp="2026-10-23T10:00:09Z"),
    )
    subagents = tmp_path / "sess" / "subagents"
    subagents.mkdir(parents=True)
    _write(
        subagents / "agent-a.jsonl",
        _assistant("msg_2", _tool("t2", "Grep"), output=100, stamp="2026-10-23T10:00:05Z"),
    )
    found, state = usage.scan(transcript)
    assert found.models[MODEL].requests == 3
    assert found.tools == {"Task": 1, "Grep": 1, "Edit": 1}
    assert found.subagent_calls == 1
    # The subagent's search came before the main agent's edit, so it is exploration.
    assert state.before_edit_tool_calls == 2
    assert state.before_edit_tokens == (2 + 5) + (2 + 100) + (2 + 5)


# --- the exploration metric -------------------------------------------------------


def test_tokens_and_calls_before_the_first_edit(tmp_path: Path) -> None:
    transcript = tmp_path / "s.jsonl"
    _write(
        transcript,
        _assistant("msg_1", _tool("t1", "Grep"), _tool("t2", "Read"), cache_read=100, output=10),
        _result("t1", "found"),
        _assistant("msg_2", _tool("t3", "Read"), _tool("t4", "Edit"), cache_read=200, output=20),
        _assistant("msg_3", _tool("t5", "Bash", command="pytest"), cache_read=300, output=30),
    )
    _, state = usage.scan(transcript)
    assert state.edited is True
    assert state.before_edit_tool_calls == 3  # Grep, Read, Read — not the Edit itself
    assert state.before_edit_tokens == (2 + 100 + 10) + (2 + 200 + 20)  # through the edit's request
    assert state.first_edit()["edited"] is True


@pytest.mark.parametrize("tool", ["Edit", "Write", "NotebookEdit"])
def test_every_editing_tool_ends_exploration(tmp_path: Path, tool: str) -> None:
    transcript = tmp_path / "s.jsonl"
    _write(transcript, _assistant("msg_1", _tool("t1", tool)))
    assert usage.scan(transcript)[1].edited


def test_a_session_that_never_edits_counts_everything_as_exploration(tmp_path: Path) -> None:
    transcript = tmp_path / "s.jsonl"
    _write(transcript, _assistant("msg_1", _tool("t1", "Read"), output=10))
    _, state = usage.scan(transcript)
    assert state.edited is False
    assert state.before_edit_tool_calls == 1
    assert state.before_edit_tokens == 12


def test_tool_result_characters_are_counted_text_parts_only(tmp_path: Path) -> None:
    transcript = tmp_path / "s.jsonl"
    as_list = _result("t2", "")
    as_list["message"]["content"][0]["content"] = [
        {"type": "text", "text": "abc"},
        {"type": "image", "source": {"data": "x" * 1000}},
    ]
    _write(transcript, _result("t1", "12345"), as_list)
    assert usage.scan(transcript)[0].tool_result_chars == 5 + 3


# --- aisquare's own footprint ---------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "aisquare repo find percentile",
        "cd src && asq repo outline core/usage.py",
        "/opt/bin/aisquare repo usages scan | head",
        "out=$(aisquare repo card x)",
        "AISQUARE_HOME=/tmp/h aisquare repo status",
    ],
)
def test_a_bash_lookup_through_the_cli_is_a_lookup(command: str) -> None:
    assert usage.is_lookup_call("Bash", {"command": command})


@pytest.mark.parametrize(
    "command",
    [
        "grep -rn aisquare repo/",
        "git log -- aisquare",
        "aisquare log",
        "ls ~/aisquare-repo",
        "echo asq repo",
    ],
)
def test_other_commands_that_mention_aisquare_are_not(command: str) -> None:
    assert not usage.is_lookup_call("Bash", {"command": command})


def test_an_aisquare_mcp_tool_is_a_lookup_and_another_servers_is_not() -> None:
    assert usage.is_lookup_call("mcp__aisquare__repo_find", {})
    assert usage.is_lookup_call("mcp__plugin_aisquare_aisquare__card", {})
    assert not usage.is_lookup_call("mcp__github__search", {})
    assert not usage.is_lookup_call("Read", {"file_path": "aisquare repo"})


@pytest.mark.parametrize(
    ("command", "ours"),
    [
        ("/Users/x/.local/share/uv/tools/aisquare-cli/bin/asq hook session-start", True),
        ("aisquare hook user-prompt-submit", True),
        ("'/venv/bin/python' -I -m aisquare hook stop", True),
        ('sh "${CLAUDE_PLUGIN_ROOT}/scripts/aisquare-hook" session-start', True),
        ("/Users/x/.config/iterm2/cc-status", False),
        ("python ~/aisquare/scripts/lint.py hook", False),
    ],
)
def test_aisquare_hooks_are_known_by_their_program(command: str, ours: bool) -> None:
    assert usage.is_aisquare_hook(command) is ours


def test_injected_characters_count_aisquare_context_hooks_only(tmp_path: Path) -> None:
    transcript = tmp_path / "s.jsonl"
    _write(
        transcript,
        _hook(
            "SessionStart", "asq hook session-start", "<aisquare-context>12345</aisquare-context>"
        ),
        _hook("UserPromptSubmit", "asq hook user-prompt-submit", "abc"),
        _hook("SessionStart", "/other/tool", "not ours, not counted"),
        _hook("Stop", "asq hook stop", '{"decision": "block", "reason": "wake up"}'),
        _hook("SessionEnd", "asq hook session-end", "bookkeeping"),
    )
    found = usage.scan(transcript)[0]
    assert found.injected_chars == len("<aisquare-context>12345</aisquare-context>") + 3 + 7


# --- the isolation traces ---------------------------------------------------------


def test_a_native_transcript_has_no_traces_even_when_it_reads_this_repo(tmp_path: Path) -> None:
    """A tool's OUTPUT may quote ``<aisquare-context>`` (this repository is about it);
    that is the repo being read, not aisquare reaching the model."""
    transcript = tmp_path / "s.jsonl"
    _write(
        transcript,
        _assistant("msg_1", _tool("t1", "Grep", pattern="aisquare-context")),
        _result("t1", 'src/aisquare/core/injection.py: "<aisquare-context>"'),
        _hook("SessionStart", "/other/tool", "nothing of ours"),
    )
    assert usage.scan(transcript)[0].aisquare_traces == []


def test_every_way_aisquare_can_reach_a_session_leaves_a_trace(tmp_path: Path) -> None:
    transcript = tmp_path / "s.jsonl"
    prompt = {
        "type": "user",
        "message": {"role": "user", "content": "<aisquare-context>x</aisquare-context> do it"},
    }
    offered = {
        "type": "attachment",
        "attachment": {"type": "deferred_tools_delta", "addedNames": ["mcp__aisquare__card"]},
    }
    _write(
        transcript,
        _hook("SessionStart", "asq hook session-start", ""),
        _hook("UserPromptSubmit", "/elsewhere/hook", "<aisquare-team-delta>…"),
        prompt,
        offered,
        _assistant("msg_1", _tool("t1", "mcp__aisquare__card")),
    )
    traces = usage.scan(transcript)[0].aisquare_traces
    assert "aisquare hook ran: SessionStart" in traces
    assert "aisquare context in UserPromptSubmit hook output" in traces
    assert "aisquare context in a user message" in traces
    assert "aisquare MCP offered: mcp__aisquare__card" in traces
    assert "aisquare MCP tool called: mcp__aisquare__card" in traces


# --- the hot path -------------------------------------------------------------------


def test_scanning_a_five_megabyte_transcript_fits_the_stop_hooks_budget(tmp_path: Path) -> None:
    """The Stop hook's read: under 50 ms p95 is the contract on the machine it was
    measured on (the review note gives the figures); this bound is an order of
    magnitude over it, to catch a quadratic or a whole-file reparse per Stop."""
    transcript = tmp_path / "big.jsonl"
    filler = "x" * 4_000
    with transcript.open("w", encoding="utf-8") as handle:
        for n in range(600):
            handle.write(json.dumps(_assistant(f"msg_{n}", _tool(f"t{n}", "Read"))) + "\n")
            handle.write(json.dumps(_result(f"t{n}", filler)) + "\n")
            handle.write(json.dumps({"type": "api-request-blob", "blob": filler}) + "\n")
    assert transcript.stat().st_size > 5_000_000
    began = time.perf_counter()
    found, state = usage.scan(transcript)
    cold = time.perf_counter() - began
    assert found.models[MODEL].requests == 600
    _write(transcript, _assistant("msg_new", output=1), _result("t_new", filler), mode="a")
    began = time.perf_counter()
    usage.scan(transcript, state)
    incremental = time.perf_counter() - began
    assert cold < budget(1.0)
    assert incremental < budget(0.05)


# --- #46: the #45 review's n2, n4, n5 --------------------------------------------------


def test_a_line_under_an_aisquare_path_is_not_parsed_for_its_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#45, n2: every transcript line carries its cwd, and a bare ``aisquare`` marker matched
    it for any session under a path naming aisquare (every fleet worktree), so the bytes
    pre-filter skipped nothing. A line with nothing to count is not parsed; one with the
    context markers still is."""
    transcript = tmp_path / "s.jsonl"
    cwd = "/Users/me/aisquare-cli/.aisquare-worktrees/coder-1"
    title = {"type": "summary", "summary": "a title", "cwd": cwd}
    marked = {
        "type": "user",
        "cwd": cwd,
        "message": {"role": "user", "content": "<aisquare-context>hi</aisquare-context>"},
    }
    _write(transcript, title, marked)
    parsed: list[str] = []
    real = usage.json.loads

    def loads(raw: Any, *args: Any, **kwargs: Any) -> Any:
        parsed.append(raw if isinstance(raw, str) else raw.decode())
        return real(raw, *args, **kwargs)

    monkeypatch.setattr(usage.json, "loads", loads)
    found, _ = usage.scan(transcript)
    monkeypatch.undo()
    assert [json.loads(line)["type"] for line in parsed] == ["user"]
    assert found.aisquare_traces == ["aisquare context in a user message"]


def test_a_rewritten_transcript_longer_than_the_dedupe_counts_nothing_twice(
    tmp_path: Path,
) -> None:
    """#45, n4: a file that shrank is read again from its start, and the dedupe by id
    remembers only the last ``_RECENT_IDS``: a longer session counted its older requests a
    second time. Read again, it counts only what is newer than it read before."""
    transcript = tmp_path / "s.jsonl"
    many = usage._RECENT_IDS + 88
    stamps = [f"2026-10-23T10:{n // 60:02d}:{n % 60:02d}.000Z" for n in range(many)]
    entries = [_assistant(f"msg_{n}", output=1, stamp=stamps[n]) for n in range(many)]
    padding = {"type": "summary", "summary": "x" * 4000}
    _write(transcript, padding, *entries)
    first, state = usage.scan(transcript)
    assert first.models[MODEL].requests == many

    later = _assistant("msg_new", output=7, stamp="2026-10-23T11:00:00.000Z")
    _write(transcript, *entries, later)  # rewritten without the padding: it shrank
    second, state = usage.scan(transcript, state)
    assert second.models[MODEL].requests == 1, "the new request alone"
    assert second.models[MODEL].output == 7
    third, _ = usage.scan(transcript, usage.ScanState.parse(state.to_json()))
    assert third.models == {}


def test_an_increase_is_priced_at_its_request_s_long_prompt_tier(tmp_path: Path) -> None:
    """#45, n5: a later line raising a counted request's output was priced as a request of
    its own, prompt ~0, so a long prompt's increase read at the short-prompt rate. It is
    priced at the request's tier: the two lines cost what the final figures do."""
    from aisquare.core import pricing

    model = "claude-haiku-5-5"  # 0.10/0.50 per MTok, 0.50/2.50 over a 100k prompt
    long_prompt = 150_000
    transcript = tmp_path / "s.jsonl"
    _write(
        transcript,
        _assistant("msg_1", model=model, input_tokens=long_prompt, output=10),
        _assistant("msg_1", model=model, input_tokens=long_prompt, output=1_000),
    )
    found, _ = usage.scan(transcript)
    whole = pricing.cost_usd(model, pricing.RequestTokens(input=long_prompt, output=1_000))
    assert whole is not None
    assert found.models[model].cost_usd == pytest.approx(whole)
