"""``metrics ledger`` reads every agent's sessions through its own transcript reader (#113's
#17: #296's WS-0 read Claude Code's transcripts alone).

The rules are one (``core.ledger``); where a request, a wake, a delta and a compaction sit
is each agent's (``AgentTranscript.ledger``). A Codex rollout is built here record by record
in the shapes Codex 0.164.0 writes (``agent_adapters.codex_ledger``), every expected number
worked out beside it; the measured rollout of ``tests/fixtures/agent_transcripts`` is read
too; and the synthetic third agents of ``test_third_agent_seams`` are counted by theirs.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aisquare.core.agent_adapters import get_adapter, register, unregister
from aisquare.core.ledger import DELTA_CLOSE, WAKE_MARKER
from aisquare.models import TeamSession
from aisquare.services import token_ledger
from tests.fake_agent import FakeAgentAdapter
from tests.gemini_shaped_agent import GeminiShapedAdapter

_T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
_FIXTURES = Path(__file__).parent / "fixtures" / "agent_transcripts"
DELTA = f'<aisquare-team-delta board="prj_a">\n#7 coder-1 note: rebased\n{DELTA_CLOSE}'


def _at(seconds: int) -> str:
    return (_T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _rec(seconds: int, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"timestamp": _at(seconds), "type": kind, "payload": payload}


def _message(seconds: int, role: str, text: str) -> dict[str, Any]:
    content = [{"type": "input_text" if role != "assistant" else "output_text", "text": text}]
    return _rec(seconds, "response_item", {"type": "message", "role": role, "content": content})


def _billed(seconds: int, rid: str, running: int, fresh: int, read: int, out: int) -> dict:
    usage = {
        "input_tokens": fresh + read,  # Codex's input HOLDS the cached ones
        "cached_input_tokens": read,
        "cache_write_input_tokens": 0,
        "output_tokens": out,
        "reasoning_output_tokens": 0,
        "total_tokens": fresh + read + out,
    }
    return _rec(
        seconds,
        "token_usage_record",
        {
            "thread_id": "thr_1",
            "turn_id": "turn_1",
            "response_id": rid,
            "usage": usage,
            "thread_token_usage": {"total_tokens": running},
        },
    )


def _call(seconds: int, name: str, cmd: str | None = None, namespace: str = "functions") -> dict:
    arguments = json.dumps({"cmd": cmd} if cmd is not None else {})
    payload = {
        "type": "function_call",
        "name": name,
        "namespace": namespace,
        "call_id": f"call_{seconds}",
        "arguments": arguments,
    }
    return _rec(seconds, "response_item", payload)


def _task_complete(seconds: int) -> dict[str, Any]:
    return _rec(seconds, "event_msg", {"type": "task_complete", "turn_id": "turn_1"})


def _codex_rollout(path: Path) -> Path:
    """One Codex session: a prompt and its delta, two requests (one written twice), a
    wake that only reads the board, a wake that acts, a compaction, and a request after."""
    records = [
        _rec(0, "session_meta", {"id": "thr_1", "cwd": "/w/alpha"}),
        _message(1, "developer", DELTA),  # the prompt hook's context: one delta
        _message(1, "user", "start on #7"),
        _billed(2, "resp_1", 1100, fresh=400, read=600, out=100),  # r1: context 1000
        _billed(2, "resp_1", 1100, fresh=400, read=600, out=100),  # the same record again
        _call(3, "exec_command", "git status"),
        _billed(4, "resp_2", 2300, fresh=300, read=800, out=100),  # r2: context 1100
        _task_complete(5),
        _message(6, "user", f"Stop hook: 2 {WAKE_MARKER}"),  # wake 1
        _call(7, "exec_command", "aisquare board --json | head -40"),  # a board read alone
        _billed(8, "resp_3", 3500, fresh=200, read=1000, out=0),  # r3: context 1200
        _task_complete(9),
        _message(10, "user", f"Stop hook: 1 {WAKE_MARKER}"),  # wake 2
        _call(11, "apply_patch"),  # acts
        _call(12, "team_board", namespace="mcp__aisquare"),
        _billed(13, "resp_4", 4800, fresh=100, read=1200, out=0),  # r4: context 1300
        _task_complete(14),
        _rec(15, "compacted", {"message": "summary"}),
        _billed(16, "resp_5", 5300, fresh=500, read=0, out=0),  # r5: context 500
    ]
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + '\n{"timestamp": "2026', encoding="utf-8"
    )
    return path


def _session(path: Path, agent: str | None, sid: str = "sess-1") -> TeamSession:
    return TeamSession(
        id=sid,
        project_id="prj_test",
        role="manager",
        started_at=_T0,
        last_seen_at=_T0,
        transcript_path=str(path),
        agent=agent,
    )


def test_a_codex_rollout_gives_the_hand_computed_numbers(tmp_path: Path) -> None:
    (row,) = token_ledger.ledger([_session(_codex_rollout(tmp_path / "r.jsonl"), "codex")])

    assert row.transcript_found and row.session_id == "sess-1" and row.role == "manager"
    assert row.requests == 5, "resp_1 written twice is one request"
    assert (row.input_fresh, row.input_cache_write, row.input_cache_read) == (1500, 0, 3600)
    assert row.output == 200
    assert row.baseline == 1000
    assert (row.peak_context, row.latest_context) == (1300, 500)
    assert row.mean_context == round((1000 + 1100 + 1200 + 1300 + 500) / 5)
    assert (row.wakes, row.no_action_wakes) == (2, 1)
    assert row.wake_input == 1200 + 1300
    assert (row.delta_blocks, row.delta_chars) == (1, len(DELTA))
    assert row.delta_carried_chars == len(DELTA) * 4, "r1..r4, until the compaction"
    assert row.compactions == 1 and row.compaction_triggers == {"unknown": 1}
    assert row.tool_calls == {"apply_patch": 1, "exec_command": 2, "mcp__aisquare__team_board": 1}
    assert row.skipped_lines == 1


def test_since_and_until_count_a_codex_window_and_keep_its_baseline(tmp_path: Path) -> None:
    path = _codex_rollout(tmp_path / "r.jsonl")
    (row,) = token_ledger.ledger(
        [_session(path, "codex")],
        since=_T0 + timedelta(seconds=3),
        until=_T0 + timedelta(seconds=9),
    )
    assert row.requests == 2  # r2, r3
    assert row.baseline == 1000, "the session's first request, whatever the window"


def test_the_measured_codex_rollout_reads_as_its_records_say() -> None:
    """Codex 0.164.0 on a loopback provider (SCOPE #113 main-274, N1): two responses of
    1000 and 2000 input tokens, 600 and 1200 of them cached, 51 and 52 out."""
    path = _FIXTURES / "codex-usage-0.164.0.jsonl"
    (row,) = token_ledger.ledger([_session(path, "codex")])
    assert row.requests == 2
    assert (row.input_fresh, row.input_cache_read, row.output) == (400 + 800, 600 + 1200, 103)
    assert (row.baseline, row.peak_context, row.latest_context) == (1000, 2000, 2000)
    assert row.skipped_lines == 0


def test_codex_reads_its_context_and_its_compaction(tmp_path: Path) -> None:
    path = _codex_rollout(tmp_path / "r.jsonl")
    reader = get_adapter("codex").transcript_reader
    assert reader is not None
    assert reader.context_tokens(path) == 500
    assert reader.compacted_since(path, _T0 + timedelta(seconds=15))
    assert not reader.compacted_since(path, _T0 + timedelta(seconds=16))


def test_a_session_of_an_agent_this_install_does_not_know_is_a_row_of_zeros(
    tmp_path: Path,
) -> None:
    (row,) = token_ledger.ledger([_session(_codex_rollout(tmp_path / "r.jsonl"), "no-such")])
    assert (row.transcript_found, row.requests) == (False, 0)


@pytest.fixture(params=[FakeAgentAdapter, GeminiShapedAdapter], ids=lambda cls: cls.id)
def third(request: pytest.FixtureRequest) -> Iterator[Any]:
    adapter = request.param()
    register(adapter)
    try:
        yield adapter
    finally:
        unregister(adapter.id)


def test_a_third_agents_session_is_counted_by_its_own_reader(third: Any, tmp_path: Path) -> None:
    """Each synthetic agent's own records (``tests/fixtures/agent_transcripts/<id>.json``'s
    shapes): one request of 30 fresh and 70 reused, 9 out, and one tool call."""
    path = tmp_path / "t.jsonl"
    if third.id == FakeAgentAdapter.id:
        records: list[dict[str, Any]] = [
            {"kind": "brief", "text": DELTA},
            {"kind": "billed", "model": "fake-1", "fresh": 30, "reused": 70, "out": 9},
            {"kind": "run", "tool": "shell", "id": "c1"},
        ]
    else:
        records = [
            {
                "type": "gemini",
                "id": "g1",
                "model": "gemini-x",
                "tokens": {"input": 100, "cached": 70, "output": 9},
                "toolCalls": [{"id": "c1", "name": "run_shell_command", "args": {}}],
            }
        ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    (row,) = token_ledger.ledger([_session(path, third.id)])

    assert (row.requests, row.input_fresh, row.input_cache_read, row.output) == (1, 30, 70, 9)
    assert row.baseline == 100 and sum(row.tool_calls.values()) == 1
