"""The token ledger: what each session spent, counted from its Claude Code transcript.

The P1 handoff's ruler (``docs/plans/handoff-p1-token-diet.md`` §1.7): every
workstream reports before/after numbers counted this one way, so the numbers can be
compared and summed. The fixture below is built by hand, entry by entry, in the shapes
Claude Code 2.1.309 writes, and every expected number is worked out beside it.
"""

from __future__ import annotations

import json
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import transcripts
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.models import TeamSession
from aisquare.services import token_ledger

_T0 = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


@pytest.fixture
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


DELTA = "<aisquare-team-delta>\n1 teammate update since your last prompt\n</aisquare-team-delta>"


def _at(minutes: int) -> str:
    return (_T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _request(
    mid: str, minute: int, usage: tuple[int, int, int, int], *blocks: dict[str, Any]
) -> dict[str, Any]:
    fresh, write, read, out = usage
    return {
        "type": "assistant",
        "timestamp": _at(minute),
        "message": {
            "id": mid,
            "model": "claude-opus-5-5",
            "content": list(blocks) or [{"type": "text", "text": "ok"}],
            "usage": {
                "input_tokens": fresh,
                "cache_creation_input_tokens": write,
                "cache_read_input_tokens": read,
                "output_tokens": out,
            },
        },
    }


def _bash(tool_id: str, command: str) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": command}}


def _result(tool_id: str, minute: int) -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": _at(minute),
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "x"}]},
    }


def _wake(minute: int, count: int = 2) -> dict[str, Any]:
    return {
        "type": "user",
        "isMeta": True,
        "timestamp": _at(minute),
        "message": {
            "role": "user",
            "content": f"Stop hook feedback:\n{count} board update(s) from your fleet arrived "
            "while you were finishing this turn:\n- …",
        },
    }


def _turn_end(minute: int) -> dict[str, Any]:
    return {"type": "system", "subtype": "stop_hook_summary", "timestamp": _at(minute)}


def _blocked_stop(minute: int) -> list[dict[str, Any]]:
    """What Claude Code writes AFTER a wake's own entry: the Stop hook that blocked,
    and that Stop's summary (read off a baseline manager's transcript, 2.1.309)."""
    return [
        {
            "type": "attachment",
            "timestamp": _at(minute),
            "attachment": {"type": "hook_blocking_error", "hookEvent": "Stop"},
        },
        _turn_end(minute),
    ]


def _fixture(path: Path) -> Path:
    """One manager session, by hand:

    =====  ==============================================  ======  ========
    req    what                                            input   output
    =====  ==============================================  ======  ========
    r1     first request (two entries, one message id)     1,010   50
    r2     after a board read                              1,105   20
    --     wake 1 (2 updates), then its blocked Stop's summary
    r3     board read only                                 1,103   10
    r4                                                     1,152    5
    --     wake 2 (wake 1 took no action), its Stop's summary
    r5     a board WRITE (`aisquare note`)                 1,152   30
    --     turn end; compaction (auto)
    r6     after the compaction                            2,001    5
    --     a synthetic entry (all zeros), a truncated last line
    =====  ==============================================  ======  ========
    """
    entries: list[dict[str, Any]] = [
        {"type": "user", "timestamp": _at(0), "message": {"content": "start the fleet"}},
        {
            "type": "attachment",
            "timestamp": _at(0),
            "attachment": {
                "type": "hook_success",
                "hookEvent": "UserPromptSubmit",
                "content": DELTA,
            },
        },
        _request("msg_1", 1, (10, 1000, 0, 50), {"type": "text", "text": "looking"}),
        _request("msg_1", 1, (10, 1000, 0, 50), _bash("t1", "aisquare task list")),
        _result("t1", 1),
        _request("msg_2", 2, (5, 100, 1000, 20)),
        _wake(3),
        *_blocked_stop(3),
        _request("msg_3", 3, (3, 0, 1100, 10), _bash("t2", "aisquare --json board")),
        _result("t2", 3),
        _request("msg_4", 4, (2, 50, 1100, 5)),
        _wake(5, count=1),
        *_blocked_stop(5),
        _request("msg_5", 5, (2, 0, 1150, 30), _bash("t3", "aisquare note 'merged #12'")),
        _result("t3", 5),
        _turn_end(6),
        {
            "type": "system",
            "subtype": "compact_boundary",
            "timestamp": _at(7),
            "compactMetadata": {"trigger": "auto", "preTokens": 1152},
        },
        _request("msg_6", 8, (1, 2000, 0, 5)),
        {
            "type": "assistant",
            "isApiErrorMessage": True,
            "timestamp": _at(9),
            "message": {"model": "<synthetic>", "usage": {"input_tokens": 0, "output_tokens": 0}},
        },
    ]
    lines = [json.dumps(entry) for entry in entries]
    path.write_text("\n".join(lines) + '\n{"type": "assistant", "mess', encoding="utf-8")
    return path


def _session(
    path: Path | None, *, role: str = "manager", sid: str = "sess-manager-1"
) -> TeamSession:
    return TeamSession(
        id=sid,
        project_id="prj_test",
        role=role,
        started_at=_T0,
        last_seen_at=_T0,
        transcript_path=str(path) if path is not None else None,
    )


def test_the_fixture_gives_the_hand_computed_numbers(tmp_path: Path) -> None:
    (row,) = token_ledger.ledger([_session(_fixture(tmp_path / "t.jsonl"))])
    assert row.transcript_found is True
    assert row.requests == 6  # r1's two entries share one message id
    assert (row.input_fresh, row.input_cache_write, row.input_cache_read) == (23, 3150, 4350)
    assert row.input_total == 7523 == 1010 + 1105 + 1103 + 1152 + 1152 + 2001
    assert row.output == 120
    assert row.baseline == 1010
    assert (row.peak_context, row.latest_context) == (2001, 2001)
    assert row.mean_context == 1254  # 7,523 / 6, rounded
    assert (row.wakes, row.no_action_wakes) == (2, 1)
    assert row.wake_input == 1103 + 1152 + 1152
    assert (row.delta_blocks, row.delta_chars) == (1, len(DELTA))
    assert row.delta_carried_chars == len(DELTA) * 5  # r1..r5, until the compaction
    assert row.compactions == 1 and row.compaction_triggers == {"auto": 1}
    assert row.tool_calls == {"Bash": 3}
    assert row.skipped_lines == 1  # the truncated last line


def test_a_truncated_last_line_is_skipped_and_the_rest_counted(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text(
        json.dumps(_request("msg_1", 1, (1, 2, 3, 4))) + '\n{"type": "assist', encoding="utf-8"
    )
    (row,) = token_ledger.ledger([_session(path)])
    assert (row.requests, row.input_total, row.output, row.skipped_lines) == (1, 6, 4, 1)


def test_since_and_until_count_only_what_happened_between(tmp_path: Path) -> None:
    path = _fixture(tmp_path / "t.jsonl")
    (row,) = token_ledger.ledger(
        [_session(path)], since=_T0 + timedelta(minutes=3), until=_T0 + timedelta(minutes=6)
    )
    assert row.requests == 3  # r3, r4, r5
    assert row.baseline == 1010, "the baseline is the session's first request, whatever the window"
    assert (row.wakes, row.no_action_wakes) == (2, 1)


def test_a_session_with_no_transcript_is_a_row_of_zeros(tmp_path: Path) -> None:
    rows = token_ledger.ledger([_session(None), _session(tmp_path / "gone.jsonl", sid="s2")])
    assert [row.transcript_found for row in rows] == [False, False]
    assert all(row.requests == 0 and row.baseline is None for row in rows)


def test_the_total_sums_every_row(tmp_path: Path) -> None:
    path = _fixture(tmp_path / "t.jsonl")
    rows = token_ledger.ledger([_session(path), _session(path, role="coder", sid="sess-coder-1")])
    total = token_ledger.total(rows)
    assert (total.requests, total.input_total, total.wakes) == (12, 2 * 7523, 4)
    assert total.tool_calls == {"Bash": 6}
    assert total.peak_context == 2001


def test_a_large_transcript_streams_in_bounded_memory(tmp_path: Path) -> None:
    path = tmp_path / "big.jsonl"
    line = json.dumps(_request("msg_x", 1, (1, 1, 1, 1), {"type": "text", "text": "y" * 400}))
    with path.open("w", encoding="utf-8") as handle:
        for index in range(30_000):
            handle.write(line.replace("msg_x", f"msg_{index}") + "\n")
    assert path.stat().st_size > 15_000_000
    tracemalloc.start()
    try:
        (row,) = token_ledger.ledger([_session(path)])
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert row.requests == 30_000
    # A constant: the baseline's head read (1 MB, `transcripts._HEAD_BYTES`) and one line.
    assert peak < 4_000_000, f"read {path.stat().st_size} bytes holding {peak} at once"


def test_latest_context_reads_the_last_request_from_the_tail(tmp_path: Path) -> None:
    path = _fixture(tmp_path / "t.jsonl")
    assert transcripts.latest_context_tokens(path) == 2001  # not the synthetic zeros after it
    assert transcripts.latest_context_tokens(tmp_path / "missing.jsonl") is None


def test_metrics_tokens_prints_counts_never_text(runner: CliRunner, work_dir: Path) -> None:
    path = _fixture(work_dir / "t.jsonl")
    project = team_project(work_dir)
    with store_session() as store:
        store.ensure_project(project)
        session = _session(path).model_copy(update={"project_id": project.id})
        store.upsert_session(session)

    result = runner.invoke(app, ["--json", "metrics", "tokens"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    (row,) = payload["sessions"]
    assert row["requests"] == 6 and row["input_total"] == 7523 and row["role"] == "manager"
    assert payload["total"]["requests"] == 6
    assert "start the fleet" not in result.stdout and "merged #12" not in result.stdout
    assert str(path) not in result.stdout, "a transcript's path is not a count"

    table = runner.invoke(app, ["metrics", "tokens", "--role", "manager"])
    assert table.exit_code == 0, table.output
    assert "7,523" in table.stdout and "total" in table.stdout
    none = runner.invoke(app, ["--json", "metrics", "tokens", "--role", "coder"])
    assert json.loads(none.stdout)["sessions"] == []
