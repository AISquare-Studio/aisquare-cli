"""Session token usage, recorded by the hooks: every Stop reads what its turn added.

The other half of ``tests/test_usage.py``: that file holds the transcript reader
to its counts; this one holds the hooks to calling it, the store to keeping its
place, and the whole path to never costing a session anything when it fails.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import paths
from aisquare.core.store import store_session
from aisquare.services import hooks as hooks_service
from aisquare.services import metrics as metrics_service
from tests.budgets import budget

pytestmark = pytest.mark.usefixtures("project_capture")

SESSION = "11111111-2222-3333-4444-555555555555"


def _assistant(message_id: str, tool: str | None = None, *, read: int = 1000) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": "ok"}]
    if tool:
        content = [{"type": "tool_use", "id": f"t_{message_id}", "name": tool, "input": {}}]
    return {
        "type": "assistant",
        "timestamp": "2026-10-23T10:00:00Z",
        "message": {
            "id": message_id,
            "model": "claude-sonnet-5-5",
            "content": content,
            "usage": {
                "input_tokens": 5,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": read,
                "output_tokens": 50,
            },
        },
    }


def _append(path: Path, *entries: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


def _turn(tmp_path: Path, transcript: Path, *entries: dict[str, Any]) -> None:
    """One turn as Claude Code drives it: the prompt hook, the reply, the Stop hook."""
    hooks_service.prompt_submitted("do it", tmp_path, session_id=SESSION)
    _append(transcript, *entries)
    hooks_service.turn_stopped(tmp_path, session_id=SESSION, transcript_path=str(transcript))


def test_stop_records_the_turns_usage_and_fills_its_metric_row(tmp_path: Path) -> None:
    transcript = tmp_path / f"{SESSION}.jsonl"
    _turn(tmp_path, transcript, _assistant("m1", "Read"), _assistant("m1", "Read"))
    with store_session() as store:
        session = store.session_usage(SESSION)
        turns = store.turn_usages(SESSION)
        metric = store.turn_metrics(session_id=SESSION)[0]
    assert session is not None
    assert session.turns == 1
    assert session.total_tokens == 5 + 100 + 1000 + 50
    assert session.usage["tools"] == {"Read": 1}
    assert len(turns) == 1
    assert turns[0].trigger == "stop"
    assert turns[0].trace_id == metric.trace_id
    assert turns[0].scan_ms is not None
    assert (metric.tokens_in, metric.tokens_out, metric.tool_calls) == (1105, 50, 1)


def test_each_stop_reads_only_its_own_turn(tmp_path: Path) -> None:
    transcript = tmp_path / f"{SESSION}.jsonl"
    _turn(tmp_path, transcript, _assistant("m1", "Read", read=1000))
    _turn(tmp_path, transcript, _assistant("m2", "Edit", read=2000))
    with store_session() as store:
        session = store.session_usage(SESSION)
        turns = store.turn_usages(SESSION)
    assert [turn.total_tokens for turn in turns] == [1155, 2155]
    assert [turn.seq for turn in turns] == [1, 2]
    assert session is not None
    assert session.total_tokens == 1155 + 2155
    assert session.usage["first_edit"] == {
        "edited": True,
        "tokens": 1155 + 2155,
        "weighted": pytest.approx(session.usage["first_edit"]["weighted"]),
        "tool_calls": 1,
    }


def test_a_stop_with_nothing_new_writes_no_turn(tmp_path: Path) -> None:
    transcript = tmp_path / f"{SESSION}.jsonl"
    _turn(tmp_path, transcript, _assistant("m1"))
    hooks_service.turn_stopped(tmp_path, session_id=SESSION, transcript_path=str(transcript))
    with store_session() as store:
        assert len(store.turn_usages(SESSION)) == 1


def test_session_end_records_the_last_stretch_and_marks_the_session_ended(tmp_path: Path) -> None:
    transcript = tmp_path / f"{SESSION}.jsonl"
    _turn(tmp_path, transcript, _assistant("m1"))
    _append(transcript, _assistant("m2"))  # an /exit mid-turn: no Stop for this one
    hooks_service.session_ended(tmp_path, session_id=SESSION, transcript_path=str(transcript))
    with store_session() as store:
        session = store.session_usage(SESSION)
        turns = store.turn_usages(SESSION)
    assert session is not None
    assert session.ended_at is not None
    assert [turn.trigger for turn in turns] == ["stop", "session_end"]


def test_a_failed_turn_is_recorded_too(tmp_path: Path) -> None:
    transcript = tmp_path / f"{SESSION}.jsonl"
    hooks_service.prompt_submitted("do it", tmp_path, session_id=SESSION)
    _append(transcript, _assistant("m1"))
    hooks_service.turn_failed(
        session_id=SESSION, error="overloaded", transcript_path=str(transcript), cwd=tmp_path
    )
    with store_session() as store:
        assert [turn.trigger for turn in store.turn_usages(SESSION)] == ["stop_failure"]


def test_recording_never_raises_and_says_why_in_its_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(metrics_service, "_record_usage", broken)
    transcript = tmp_path / f"{SESSION}.jsonl"
    _turn(tmp_path, transcript, _assistant("m1"))  # does not raise
    log = paths.log_dir() / "metrics.log"
    assert "usage not recorded for" in log.read_text()
    assert "disk on fire" in log.read_text()


def test_a_missing_transcript_is_nothing_to_record(tmp_path: Path) -> None:
    assert (
        metrics_service.record_usage(
            SESSION, str(tmp_path / "gone.jsonl"), tmp_path, trigger="stop"
        )
        is None
    )
    assert metrics_service.record_usage(SESSION, None, tmp_path, trigger="stop") is None


def test_two_writers_of_the_same_bytes_count_them_once(tmp_path: Path) -> None:
    """A Stop and a SessionEnd that scanned from the same state: the second loses the
    compare-and-set and writes nothing."""
    transcript = tmp_path / f"{SESSION}.jsonl"
    _turn(tmp_path, transcript, _assistant("m1"))
    with store_session() as store:
        before = store.session_usage(SESSION)
    assert before is not None
    _append(transcript, _assistant("m2"))
    assert metrics_service.record_usage(SESSION, str(transcript), tmp_path, trigger="stop")
    stale = before.model_copy(update={"turns": 99})
    with store_session() as store:
        assert not store.record_usage(stale, None, previous_state=before.state)
        session = store.session_usage(SESSION)
    assert session is not None
    assert session.turns == 2


def test_metrics_tokens_sums_the_projects_sessions(
    tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    transcript = tmp_path / f"{SESSION}.jsonl"
    _turn(tmp_path, transcript, _assistant("m1", "Read"), _assistant("m2", "Edit"))
    result = runner.invoke(app, ["--json", "metrics", "tokens"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["sessions"] == 1
    assert payload["requests"] == 2
    assert payload["total_tokens"] == 2 * 1155
    assert payload["cache_read_tokens"] == 2000
    # 2 x (5 + 1.25 x 100 + 0.1 x 1000 + 5 x 50)
    assert payload["weighted_tokens"] == pytest.approx(2 * 480)
    assert payload["tools"] == {"Edit": 1, "Read": 1}
    assert payload["sessions_with_edit"] == 1
    assert payload["prices_as_of"] == "2026-10-06"
    table = runner.invoke(app, ["metrics", "tokens"])
    assert "cost-weighted" in table.stdout
    assert "saved" not in table.stdout
    one = runner.invoke(app, ["--json", "metrics", "tokens", "--session", SESSION[:8]])
    assert json.loads(one.stdout)["sessions"] == 1


def test_an_unpriced_model_is_reported_apart(
    tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    transcript = tmp_path / f"{SESSION}.jsonl"
    entry = _assistant("m1")
    entry["message"]["model"] = "claude-opus-4-5"
    _turn(tmp_path, transcript, entry)
    payload = json.loads(runner.invoke(app, ["--json", "metrics", "tokens"]).stdout)
    assert payload["unpriced_models"] == ["claude-opus-4-5"]
    assert payload["unpriced_tokens"] == 1155
    assert payload["weighted_tokens"] == 0
    assert payload["total_tokens"] == 1155


# --- held to Claude Code's own bill, and to the Stop hook's budget ------------------

_FIXTURES = Path(__file__).parent / "fixtures" / "usage"

#: What ``claude -p --output-format json`` reported as ``total_cost_usd`` for the runs
#: the fixture's requests are taken from (2026-10-23, ``costBasis: list``): 0.00146363
#: for the Haiku 5.5 request and 0.0546114 for the Sonnet 5.5 ones, split here over two
#: streamed requests (a request's price is linear in its tokens below Haiku's long rate).
_CLAUDE_CODE_TOTAL_COST_USD = 0.00146363 + 0.0546114


def test_cost_from_usage_is_within_two_percent_of_claude_codes_total_cost(
    tmp_path: Path,
) -> None:
    """The transcript's streamed lines counted once per message id, priced per model."""
    transcript = tmp_path / f"{SESSION}.jsonl"
    transcript.write_bytes((_FIXTURES / "headless-run.jsonl").read_bytes())
    hooks_service.prompt_submitted("fix the failing test", tmp_path, session_id=SESSION)
    hooks_service.turn_stopped(tmp_path, session_id=SESSION, transcript_path=str(transcript))
    with store_session() as store:
        session = store.session_usage(SESSION)
    assert session is not None
    assert session.usage["models"]["claude-sonnet-5-5"]["requests"] == 2
    assert session.cost_usd == pytest.approx(_CLAUDE_CODE_TOTAL_COST_USD, rel=0.02)


def test_the_stop_hooks_recording_stays_under_ten_milliseconds_p95(tmp_path: Path) -> None:
    """Measured on the hook's own path: read the appended bytes, write both rows."""
    transcript = tmp_path / f"{SESSION}.jsonl"
    transcript.touch()
    timings: list[float] = []
    for n in range(40):
        _append(transcript, _assistant(f"m{n}", "Read"), _assistant(f"m{n}", "Read"))
        began = time.perf_counter()
        metrics_service.record_usage(SESSION, str(transcript), tmp_path, trigger="stop")
        timings.append(time.perf_counter() - began)
    with store_session() as store:
        turns = store.turn_usages(SESSION)
    assert len(turns) == 40
    timings.sort()
    p95 = timings[int(len(timings) * 0.95) - 1]
    assert p95 < budget(0.010), f"p95 {p95 * 1000:.1f} ms"
