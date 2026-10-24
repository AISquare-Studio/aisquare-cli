"""Codex's token usage off its rollout (``codex_usage``, the transcript slot's ``usage``), in the
shapes 0.164.0 and 0.162.1 write (SCOPE #113 main-274, N1 and N2): what the conformance suite's
recorded moments do not reach."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aisquare.core.agent_adapters import codex_usage
from aisquare.core.agent_adapters.usage_tally import RECENT, Tally

STAMP = "2026-10-24T10:00:{:02d}.000Z"


def _line(n: int, kind: str, payload: dict[str, Any]) -> str:
    return json.dumps({"timestamp": STAMP.format(n), "type": kind, "payload": payload}) + "\n"


def _turn(n: int, turn: str, model: str) -> str:
    return _line(n, "turn_context", {"turn_id": turn, "model": model, "cwd": "/work"})


def _record(
    n: int,
    turn: str,
    response: str,
    *,
    input: int,
    cached: int = 0,
    written: int = 0,
    output: int = 1,
    running: int = 0,
) -> str:
    usage = {
        "input_tokens": input,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": written,
        "output_tokens": output,
        "reasoning_output_tokens": 0,
        "total_tokens": input + output,
    }
    thread = {**usage, "total_tokens": running or input + output}
    payload = {
        "turn_id": turn,
        "response_id": response,
        "usage": usage,
        "thread_token_usage": thread,
    }
    return _line(n, "token_usage_record", payload)


def _call(n: int, name: str, call: str, **extra: Any) -> str:
    payload = {"type": "function_call", "name": name, "call_id": call, "arguments": "{}", **extra}
    return _line(n, "response_item", payload)


def _scan(path: Path, *lines: str, state: str | None = None) -> Any:
    path.write_text("".join(lines), encoding="utf-8")
    return codex_usage.usage(path, state)


def test_a_record_is_priced_at_its_turns_model_and_a_turn_without_one_at_the_newest(
    tmp_path: Path,
) -> None:
    found = _scan(
        tmp_path / "r.jsonl",
        _turn(0, "t1", "gpt-5.4"),
        _record(1, "t1", "resp_a", input=1000, cached=600, output=10),
        _turn(2, "t2", "gpt-6.1-sol"),
        _record(3, "t-unseen", "resp_b", input=100, output=5),
    ).delta
    assert set(found.models) == {"gpt-5.4", "gpt-6.1-sol"}
    older = found.models["gpt-5.4"]
    assert (older.input, older.cache_read, older.output) == (400, 600, 10)
    assert older.cost_usd == pytest.approx((400 * 2.5 + 600 * 0.25 + 10 * 15.0) / 1e6)
    assert found.models["gpt-6.1-sol"].input == 100


def test_a_record_before_any_turn_context_is_counted_unpriced(tmp_path: Path) -> None:
    found = _scan(tmp_path / "r.jsonl", _record(0, "t1", "resp_a", input=7, output=3)).delta
    unknown = found.models[codex_usage.UNKNOWN_MODEL]
    assert (unknown.input, unknown.output, unknown.priced) == (7, 3, False)


def test_a_cache_write_is_its_30_minute_price_or_unpriced_where_the_page_lists_none(
    tmp_path: Path,
) -> None:
    """``cache_write_input_tokens`` is read as a part of ``input_tokens``, as the cached ones
    are, and billed at the row's 30-minute write (1.25x input); gpt-5.4 lists none."""
    found = _scan(
        tmp_path / "r.jsonl",
        _turn(0, "t1", "gpt-6.1-sol"),
        _record(1, "t1", "resp_a", input=1000, cached=600, written=100, output=10),
        _turn(2, "t2", "gpt-5.4"),
        _record(3, "t2", "resp_b", input=1000, written=100, output=10),
    ).delta
    sol = found.models["gpt-6.1-sol"]
    assert (sol.input, sol.cache_write_5m, sol.cache_read) == (300, 100, 600)
    assert sol.cost_usd == pytest.approx((300 * 2 + 100 * 2.5 + 600 * 0.1 + 10 * 10) / 1e6)
    assert found.models["gpt-5.4"].priced is False


def test_an_unpriced_request_before_the_first_edit_adds_no_weighted_tokens(tmp_path: Path) -> None:
    """The first-edit figures weigh a request as its bucket does: a cache write on a row that
    lists no write price leaves the request unpriced, so it adds tokens and no weight
    (r17 nit 5: 222.5 weighted beside a bucket priced 0.0)."""
    scan = _scan(
        tmp_path / "r.jsonl",
        _turn(0, "t1", "gpt-5.4"),
        _record(1, "t1", "resp_a", input=1000, written=100, output=10),
    )
    assert scan.delta.models["gpt-5.4"].priced is False
    assert (scan.first_edit.tokens, scan.first_edit.weighted) == (1010, 0.0)


def test_a_long_prompt_is_priced_at_the_long_context_rates(tmp_path: Path) -> None:
    found = _scan(
        tmp_path / "r.jsonl",
        _turn(0, "t1", "gpt-6.1-sol"),
        _record(1, "t1", "resp_a", input=300_000, cached=200_000, output=10),
    ).delta
    long = found.models["gpt-6.1-sol"]
    assert long.cost_usd == pytest.approx((100_000 * 4 + 200_000 * 0.2 + 10 * 15) / 1e6)


def test_token_count_lines_and_a_record_read_twice_count_nothing_more(tmp_path: Path) -> None:
    """N2: a 429 turn repeats the totals in a ``token_count`` and writes no record; a record
    keyed by its response and the thread's running total counts once, while a provider that
    reuses one id across responses has each counted."""
    count = _line(2, "event_msg", {"type": "token_count", "info": {"total_token_usage": {}}})
    record = _record(1, "t1", "resp_local", input=10, output=1, running=11)
    found = _scan(
        tmp_path / "r.jsonl",
        _turn(0, "t1", "gpt-6.1-sol"),
        record,
        count,
        record,
        _record(3, "t1", "resp_local", input=20, output=1, running=32),
    ).delta
    assert found.models["gpt-6.1-sol"].requests == 2
    assert found.totals().total == 32


def test_apply_patch_ends_exploration_after_the_response_that_called_it(tmp_path: Path) -> None:
    """The first edit's response writes its record after the call: it still counts before
    the first edit, the next one does not."""
    scan = _scan(
        tmp_path / "r.jsonl",
        _turn(0, "t1", "gpt-6.1-sol"),
        _call(1, "exec_command", "c1"),
        _record(2, "t1", "resp_a", input=10, output=1),
        _line(
            3, "response_item", {"type": "custom_tool_call", "name": "apply_patch", "call_id": "c2"}
        ),
        _record(4, "t1", "resp_b", input=20, output=2),
        _call(5, "exec_command", "c3"),
        _record(6, "t1", "resp_c", input=40, output=4),
    )
    assert scan.delta.tools == {"exec_command": 2, "apply_patch": 1}
    first = scan.first_edit
    assert (first.edited, first.tokens, first.tool_calls) == (True, 33, 1)


def test_an_aisquare_mcp_tool_or_repo_command_is_a_lookup_and_leaves_a_trace(
    tmp_path: Path,
) -> None:
    lookup = json.dumps({"cmd": "aisquare repo map"})
    found = _scan(
        tmp_path / "r.jsonl",
        _call(0, "team_board", "c1", namespace="mcp__aisquare"),
        _call(1, "exec_command", "c2", namespace="functions", arguments=lookup),
        _call(2, "search", "c3", namespace="mcp__docs"),
        _line(
            3, "response_item", {"type": "function_call_output", "call_id": "c2", "output": "abc"}
        ),
    ).delta
    assert found.tools == {
        "mcp__aisquare__team_board": 1,
        "exec_command": 1,
        "mcp__docs__search": 1,
    }
    assert found.lookup_calls == 2
    assert found.tool_result_chars == 3
    assert found.aisquare_traces == ["aisquare MCP tool called: mcp__aisquare__team_board"]


def test_a_rewritten_rollout_counts_only_what_is_newer_than_its_last_scan(tmp_path: Path) -> None:
    path = tmp_path / "r.jsonl"
    first = _scan(
        path,
        _turn(0, "t1", "gpt-6.1-sol"),
        _record(1, "t1", "resp_a", input=10, output=1),
        _record(2, "t1", "resp_b", input=20, output=1),
    )
    rewritten = _scan(
        path,
        _turn(0, "t1", "gpt-6.1-sol"),
        _record(3, "t1", "resp_c", input=40, output=1),
        state=first.state,
    )
    assert rewritten.delta.totals().total == 41


def test_the_tally_remembers_a_bounded_window_and_reads_garbage_as_fresh() -> None:
    tally = Tally(counted={str(n): [n, 0, 0, 0, 0] for n in range(RECENT + 5)})
    kept = Tally.parse(tally.to_json())
    assert len(kept.counted) == RECENT and "0" not in kept.counted
    for garbage in (None, "", "[]", "{", '{"offsets": 3}', '{"counted": {"a": "x"}}'):
        assert Tally.parse(garbage) == Tally()
