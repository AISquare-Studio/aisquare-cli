"""The transcript tail the needs-you scan classifies on (SPEC §4.3), and what it renders.

Every fixture is synthetic JSONL written from the record shapes the spec gives
for Claude Code 2.1.292: one record per content block, the blocks of one API
message sharing ``message.id``, an ``AskUserQuestion`` or ``ExitPlanMode`` as a
``tool_use`` block and its answer as a later ``tool_result``, an Esc as a user
text that starts ``[Request interrupted by user``, a rejected tool use as an
error result that says ``doesn't want to proceed``. No real transcript is read.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from aisquare.services import transcript as transcript_service
from aisquare.services.transcript import (
    MAX_LINE,
    TAIL_RECORDS,
    TOOL_INPUT_MAX,
    read_page,
    read_transcript_tail,
)

QUESTION: dict[str, Any] = {
    "questions": [
        {
            "header": "Cache",
            "question": "Which store should the cache use?",
            "multiSelect": False,
            "options": [
                {"label": "Redis", "description": "shared, needs a server"},
                {"label": "SQLite", "description": "local file"},
            ],
        }
    ]
}


def _stamp(second: int) -> str:
    return f"2026-10-07T10:{second // 60:02d}:{second % 60:02d}.000Z"


def _at(second: int) -> datetime:
    return datetime(2026, 10, 7, 10, second // 60, second % 60, tzinfo=UTC)


def _prompt(text: str, *, uuid: str, second: int = 0, **extra: Any) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": _stamp(second),
        "isSidechain": False,
        "message": {"role": "user", "content": text},
        **extra,
    }


def _said(*blocks: dict[str, Any], uuid: str, message: str | None, second: int) -> dict[str, Any]:
    body: dict[str, Any] = {"role": "assistant", "content": list(blocks), "stop_reason": None}
    if message is not None:
        body["id"] = message
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": _stamp(second),
        "isSidechain": False,
        "message": body,
    }


def _result(
    tool_use_id: str, output: str = "ok", *, uuid: str, second: int, is_error: bool = False
) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": output}
    if is_error:
        block["is_error"] = True
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": _stamp(second),
        "isSidechain": False,
        "message": {"role": "user", "content": [block]},
    }


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _tool(tool_use_id: str, name: str, **payload: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_use_id, "name": name, "input": payload}


def _write(path: Path, records: list[dict[str, Any]]) -> Path:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


def _plain(lines: list[str]) -> list[str]:
    return [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in lines]


# --- what is pending ------------------------------------------------------------------------


def test_a_pending_ask_user_question_is_read_with_its_input(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "t.jsonl",
        [
            _prompt("pick a cache", uuid="u1"),
            _said(_text("Let me ask."), uuid="a1", message="m1", second=5),
            _said(
                _tool("toolu_q", "AskUserQuestion", **QUESTION), uuid="a2", message="m1", second=6
            ),
        ],
    )
    tail = read_transcript_tail(path)
    assert tail is not None
    assert [tool.tool_use_id for tool in tail.pending] == ["toolu_q"]
    (asked,) = tail.pending
    assert asked.name == "AskUserQuestion"
    assert asked.input == QUESTION
    assert asked.at == _at(6)
    assert tail.newest == "assistant_tool"
    assert tail.newest_at == _at(6)
    assert tail.marker_key == "a2"
    assert tail.last_text == "Let me ask."


def test_an_answered_question_is_not_pending(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "t.jsonl",
        [
            _prompt("pick a cache", uuid="u1"),
            _said(
                _tool("toolu_q", "AskUserQuestion", **QUESTION), uuid="a1", message="m1", second=6
            ),
            _result("toolu_q", "User answered: SQLite", uuid="r1", second=9),
        ],
    )
    tail = read_transcript_tail(path)
    assert tail is not None and tail.pending == ()
    assert tail.newest == "tool_result"


def test_a_pending_plan_carries_the_plan(tmp_path: Path) -> None:
    plan = "# Cache plan\n\n1. Add SQLite\n2. Wire it in"
    path = _write(
        tmp_path / "t.jsonl",
        [
            _prompt("plan it", uuid="u1"),
            _said(_tool("toolu_p", "ExitPlanMode", plan=plan), uuid="a1", message="m1", second=3),
        ],
    )
    tail = read_transcript_tail(path)
    assert tail is not None
    (pending,) = tail.pending
    assert pending.name == "ExitPlanMode" and pending.input == {"plan": plan}
    _write(path, [*_records(path), _result("toolu_p", "approved", uuid="r1", second=8)])
    answered = read_transcript_tail(path)
    assert answered is not None and answered.pending == ()


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_parallel_tool_uses_are_pending_oldest_first(tmp_path: Path) -> None:
    """Tools run while the message streams: a result can land between two of its blocks."""
    path = _write(
        tmp_path / "t.jsonl",
        [
            _prompt("check three things", uuid="u1"),
            _said(_tool("toolu_a", "Bash", command="ls"), uuid="a1", message="m1", second=1),
            _said(_tool("toolu_b", "Read", file_path="/x"), uuid="a2", message="m1", second=2),
            _result("toolu_b", uuid="r1", second=3),
            _said(
                _tool("toolu_c", "Bash", command="rm -rf build"), uuid="a3", message="m1", second=4
            ),
        ],
    )
    tail = read_transcript_tail(path)
    assert tail is not None
    assert [tool.tool_use_id for tool in tail.pending] == ["toolu_a", "toolu_c"]
    assert tail.pending[0].summary == "Bash(ls)"


def test_a_result_too_long_to_parse_still_answers_its_tool_use(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "t.jsonl",
        [
            _prompt("dump it", uuid="u1"),
            _said(
                _tool("toolu_big", "Bash", command="cat huge.log"),
                uuid="a1",
                message="m1",
                second=1,
            ),
            _result("toolu_big", "x" * (MAX_LINE + 10_000), uuid="r1", second=2),
        ],
    )
    assert len(path.read_bytes().splitlines()[-1]) > MAX_LINE, "the premise: never parsed"
    tail = read_transcript_tail(path)
    assert tail is not None and tail.pending == ()


def test_sub_agent_and_injected_records_are_not_the_conversation(tmp_path: Path) -> None:
    sidechain = _said(_tool("toolu_s", "Bash", command="make"), uuid="s1", message="ms", second=9)
    sidechain["isSidechain"] = True
    meta = _prompt("Caveat: the messages below were generated by a command", uuid="m1", second=10)
    meta["isMeta"] = True
    path = _write(
        tmp_path / "t.jsonl",
        [
            _prompt("go", uuid="u1"),
            _said(_text("Done. Shall I push?"), uuid="a1", message="m1", second=5),
            sidechain,
            meta,
        ],
    )
    tail = read_transcript_tail(path)
    assert tail is not None
    assert tail.pending == ()
    assert (tail.newest, tail.marker_key) == ("assistant_text", "a1")
    assert tail.last_text == "Done. Shall I push?"


# --- how far the walk goes --------------------------------------------------------------------


def _counting(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count the records the walk parses: the measure of how much of the file it read."""
    parsed: list[int] = []
    real = transcript_service._parse_transcript_line

    def counted(raw: bytes) -> dict[str, Any] | None:
        parsed.append(len(raw))
        return real(raw)

    monkeypatch.setattr(transcript_service, "_parse_transcript_line", counted)
    return parsed


def test_the_walk_stops_at_the_message_before_the_newest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the newest message can hold a pending tool, so an older one ends the walk."""
    records: list[dict[str, Any]] = [_prompt("long task", uuid="u0")]
    for n in range(200):
        tool = _tool(f"toolu_{n}", "Bash", command=f"step {n}")
        records.append(_said(tool, uuid=f"a{n}", message=f"m{n}", second=n % 3000))
        records.append(_result(f"toolu_{n}", uuid=f"r{n}", second=n % 3000))
    records.append(_said(_text("All done?"), uuid="last", message="m200", second=3000))
    path = _write(tmp_path / "t.jsonl", records)
    parsed = _counting(monkeypatch)
    tail = read_transcript_tail(path)
    assert tail is not None and tail.newest == "assistant_text"
    assert len(parsed) == 3, "the newest message, the result before it, and the older message"


def test_the_walk_stops_at_the_humans_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Records without a message id (an older Claude Code) are read back to the prompt.

    Every tool use of the turn is looked at, since none can be told apart by
    message; the newest message's text ends at the first user record behind it.
    """
    records: list[dict[str, Any]] = []
    for n in range(200):
        records.append(_prompt(f"question {n}", uuid=f"u{n}", second=n))
        records.append(_said(_text(f"answer {n}"), uuid=f"a{n}", message=None, second=n))
    records += [
        _prompt("last", uuid="u-last", second=300),
        _said(_tool("toolu_c", "Bash", command="make"), uuid="c", message=None, second=301),
        _said(_tool("toolu_a", "Read", file_path="/x"), uuid="a", message=None, second=302),
        _result("toolu_a", uuid="r", second=303),
        _said(_text("Which one?"), uuid="q", message=None, second=304),
    ]
    path = _write(tmp_path / "t.jsonl", records)
    parsed = _counting(monkeypatch)
    tail = read_transcript_tail(path)
    assert tail is not None
    assert [tool.tool_use_id for tool in tail.pending] == ["toolu_c"]
    assert (tail.newest, tail.last_text) == ("assistant_text", "Which one?")
    assert len(parsed) == 5, "the turn, back to its prompt, and nothing older"


def test_the_walk_is_bounded_by_records_and_by_bytes(tmp_path: Path) -> None:
    """A pending tool behind more results than the backstop allows is not found — by design."""
    many: list[dict[str, Any]] = [
        _prompt("go", uuid="u0"),
        _said(_tool("toolu_old", "Bash", command="sleep"), uuid="a0", message="m1", second=1),
    ]
    many += [_result(f"other{n}", uuid=f"r{n}", second=2) for n in range(TAIL_RECORDS + 5)]
    tail = read_transcript_tail(_write(tmp_path / "records.jsonl", many))
    assert tail is not None and tail.pending == ()
    near = many[: 2 + 50]
    found = read_transcript_tail(_write(tmp_path / "near.jsonl", near))
    assert found is not None and [t.tool_use_id for t in found.pending] == ["toolu_old"]
    padded = [
        *near[:2],
        *(_result(f"p{n}", "y" * 2_000, uuid=f"p{n}", second=3) for n in range(300)),
    ]
    beyond = read_transcript_tail(_write(tmp_path / "bytes.jsonl", padded), budget=200_000)
    assert beyond is not None and beyond.pending == ()
    unbounded = read_transcript_tail(tmp_path / "bytes.jsonl", budget=10_000_000)
    assert unbounded is not None and [t.tool_use_id for t in unbounded.pending] == ["toolu_old"]


# --- what the newest record says -----------------------------------------------------------


def test_an_esc_is_an_interruption_with_its_time(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "t.jsonl",
        [
            _prompt("refactor it", uuid="u1"),
            _said(
                _text("Starting with the parser.\n\nThen the cache."),
                uuid="a1",
                message="m1",
                second=4,
            ),
            _prompt("[Request interrupted by user]", uuid="esc", second=7),
        ],
    )
    tail = read_transcript_tail(path)
    assert tail is not None
    assert (tail.newest, tail.newest_at, tail.marker_key) == ("interrupted", _at(7), "esc")
    assert tail.last_text == "Starting with the parser.\n\nThen the cache."
    assert tail.last_text_at == _at(4)


def test_a_rejected_tool_use_is_an_interruption(tmp_path: Path) -> None:
    rejection = (
        "The user doesn't want to proceed with this tool use. The tool use was rejected "
        "(eg. if it was a file edit, the new_string was NOT written to the file). STOP what "
        "you are doing and wait for the user to tell you how to proceed."
    )
    records = [
        _prompt("push it", uuid="u1"),
        _said(
            _tool("toolu_push", "Bash", command="git push --force"),
            uuid="a1",
            message="m1",
            second=2,
        ),
        _result("toolu_push", rejection, uuid="r1", second=9, is_error=True),
    ]
    tail = read_transcript_tail(_write(tmp_path / "t.jsonl", records))
    assert tail is not None and tail.pending == ()
    assert (tail.newest, tail.newest_at, tail.marker_key) == ("interrupted", _at(9), "r1")
    records.append(_prompt("[Request interrupted by user for tool use]", uuid="esc", second=9))
    marker = read_transcript_tail(_write(tmp_path / "t.jsonl", records))
    assert marker is not None and (marker.newest, marker.marker_key) == ("interrupted", "esc")


def test_an_error_result_that_is_not_a_rejection_is_a_result(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "t.jsonl",
        [
            _prompt("build", uuid="u1"),
            _said(_tool("toolu_b", "Bash", command="make"), uuid="a1", message="m1", second=2),
            _result("toolu_b", "make: *** [all] Error 2", uuid="r1", second=3, is_error=True),
        ],
    )
    tail = read_transcript_tail(path)
    assert tail is not None and tail.newest == "tool_result"


def test_the_assistants_last_words_are_its_newest_messages_text(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "t.jsonl",
        [
            _prompt("which way?", uuid="u1"),
            _said(_text("an older message"), uuid="a0", message="m0", second=1),
            _result("toolu_none", uuid="r0", second=2),
            _said({"type": "thinking", "thinking": "hmm"}, uuid="a1", message="m1", second=3),
            _said(_text("Two options:"), uuid="a2", message="m1", second=4),
            _said(_text("Which approach do you want?"), uuid="a3", message="m1", second=5),
        ],
    )
    tail = read_transcript_tail(path)
    assert tail is not None
    assert (tail.newest, tail.marker_key, tail.newest_at) == ("assistant_text", "a3", _at(5))
    assert tail.last_text == "Two options:\n\nWhich approach do you want?"
    assert tail.last_text_at == _at(5)


def test_a_message_still_thinking_does_not_decide_the_newest(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "t.jsonl",
        [
            _prompt("go", uuid="u1"),
            _said(_tool("toolu_a", "Bash", command="ls"), uuid="a1", message="m1", second=1),
            _result("toolu_a", uuid="r1", second=2),
            _said({"type": "thinking", "thinking": "next…"}, uuid="a2", message="m2", second=3),
        ],
    )
    tail = read_transcript_tail(path)
    assert tail is not None and (tail.newest, tail.marker_key) == ("tool_result", "r1")


def test_the_humans_prompt_is_the_newest_record(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "t.jsonl",
        [
            _said(_text("Anything else?"), uuid="a1", message="m1", second=1),
            _prompt("yes", uuid="u2"),
        ],
    )
    tail = read_transcript_tail(path)
    assert tail is not None and tail.newest == "user_prompt" and tail.last_text is None


def test_a_large_input_is_dropped_but_the_summary_kept(tmp_path: Path) -> None:
    content = "z" * (TOOL_INPUT_MAX + 1)
    path = _write(
        tmp_path / "t.jsonl",
        [
            _prompt("write it", uuid="u1"),
            _said(
                _tool("toolu_w", "Write", file_path="/src/big.py", content=content),
                uuid="a1",
                message="m1",
                second=1,
            ),
        ],
    )
    tail = read_transcript_tail(path)
    assert tail is not None
    (pending,) = tail.pending
    assert pending.input == {}
    assert pending.summary == "Write(/src/big.py)"


def test_no_transcript_is_none_and_an_empty_one_is_nothing(tmp_path: Path) -> None:
    assert read_transcript_tail(None) is None
    assert read_transcript_tail(tmp_path / "missing.jsonl") is None
    assert read_transcript_tail(tmp_path) is None, "a directory is unreadable, not a crash"
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    tail = read_transcript_tail(empty)
    assert tail is not None
    assert (tail.pending, tail.newest, tail.newest_at, tail.last_text) == ((), "none", None, None)


def test_a_named_pipe_is_never_opened(tmp_path: Path) -> None:
    """The path is whatever the agent's own hook payload said, and opening a FIFO waits for a
    writer: the watcher's scan would wait with it, and every later scan behind it."""
    if sys.platform == "win32":  # an `if`, not a skipif: mypy's platform check reads only this
        pytest.skip("named pipes in the file system are POSIX")
    fifo = tmp_path / "fifo.jsonl"
    os.mkfifo(fifo)
    read: list[object] = []
    reader = threading.Thread(target=lambda: read.append(read_transcript_tail(fifo)), daemon=True)
    reader.start()
    reader.join(timeout=5)
    if reader.is_alive():  # a writer lets it go, so a regression fails here instead of hanging
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        reader.join(timeout=5)
    assert read == [None], "it opened the pipe and waited on it"


def test_a_line_nested_past_the_parsers_depth_is_skipped_not_raised(tmp_path: Path) -> None:
    """``json`` raises ``RecursionError`` for it, not ``ValueError``; neither reader may."""
    path = _write(tmp_path / "deep.jsonl", [_prompt("hi", uuid="u1")])
    with path.open("a", encoding="utf-8") as handle:
        handle.write("[" * 100_000 + "]" * 100_000 + "\n")
    tail = read_transcript_tail(path)
    assert tail is not None and (tail.newest, tail.marker_key) == ("user_prompt", "u1")
    assert any("hi" in line for line in _plain(read_page(path).lines))


def test_garbage_lines_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "torn.jsonl"
    _write(
        path, [_prompt("go", uuid="u1"), _said(_text("Ready?"), uuid="a1", message="m1", second=1)]
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
        handle.write('{"type": "assistant", "message": "not an object"}\n')
        handle.write("[1, 2]\n")
    tail = read_transcript_tail(path)
    assert tail is not None and (tail.newest, tail.marker_key) == ("assistant_text", "a1")


# --- what the transcript shows -----------------------------------------------------------------


def test_a_question_renders_with_every_option(tmp_path: Path) -> None:
    multi = {
        "questions": [
            {**QUESTION["questions"][0]},
            {
                "header": "Tests",
                "question": "Which suites?",
                "multiSelect": True,
                "options": [{"label": "unit", "description": ""}, {"label": "e2e"}],
            },
        ]
    }
    path = _write(
        tmp_path / "t.jsonl",
        [_said(_tool("toolu_q", "AskUserQuestion", **multi), uuid="a1", message="m1", second=1)],
    )
    text = _plain(read_page(path).lines)
    assert "  ? Cache: Which store should the cache use?" in text
    assert "    1. Redis — shared, needs a server" in text
    assert "    2. SQLite — local file" in text
    assert "  ? Tests: Which suites? (multi-select)" in text
    assert "    1. unit" in text and "    2. e2e" in text
    assert not any("AskUserQuestion" in line for line in text), "the question, not the tool name"


def test_a_plan_renders_whole_and_wrapped(tmp_path: Path) -> None:
    plan = "# Cache plan\n\n" + "Replace the in-memory dict with a SQLite table. " * 4
    path = _write(
        tmp_path / "t.jsonl",
        [_said(_tool("toolu_p", "ExitPlanMode", plan=plan), uuid="a1", message="m1", second=1)],
    )
    text = _plain(read_page(path, width=40).lines)
    assert "  ⎿ plan:" in text
    assert "    # Cache plan" in text
    body = [
        line for line in text if line.startswith("    Replace") or line.startswith("    SQLite")
    ]
    assert body and all(len(line) <= 40 for line in text)


def test_other_tools_keep_their_one_line_summary(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "t.jsonl",
        [
            _said(
                _tool("toolu_b", "Bash", command="git status"), uuid="a1", message="m1", second=1
            ),
            _said(
                _tool("toolu_q", "AskUserQuestion", questions="not a list"),
                uuid="a2",
                message="m1",
                second=2,
            ),
        ],
    )
    text = _plain(read_page(path).lines)
    assert "  ⎿ Bash(git status)" in text
    assert "  ⎿ AskUserQuestion" in text, "a malformed question falls back to the summary"


def test_sub_agent_and_injected_records_render_nothing(tmp_path: Path) -> None:
    meta = _prompt("Caveat: generated by a local command", uuid="m1")
    meta["isMeta"] = True
    sidechain = _said(_text("a sub-agent's words"), uuid="s1", message="ms", second=2)
    sidechain["isSidechain"] = True
    path = _write(
        tmp_path / "t.jsonl", [meta, sidechain, _prompt("my own words", uuid="u1", second=3)]
    )
    text = _plain(read_page(path).lines)
    assert sum(line.startswith("> you") for line in text) == 1, "a meta record is not the human"
    assert not any("Caveat" in line or "sub-agent" in line for line in text)
    assert "  my own words" in text
