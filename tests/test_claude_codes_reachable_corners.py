"""The corners of Claude Code's adapter CI's coverage found no test in, that a user reaches.

R6 of #113 (coverage job, run 42091550486 on 9f1c7912) listed the ten largest runs of
statements no test ran in ``core/agent_adapters/claude_*.py``. These are the ones a user
reaches, each through the adapter's own reader or resolver:

- a pending ``Write``/``Edit``/``NotebookEdit`` call is marked by its file's name on Remote's
  needs-you card (``call_mark``), a path in Windows' spelling too;
- a transcript read while Claude Code is mid-line (a line that is no JSON yet) is read past,
  for the agent's last words (``reply_since``) and for who steered (``facts``);
- a Stop hook of the user's own whose output is no JSON puts nothing in front of the model
  (``usage``'s injected characters), where aisquare's ``block`` puts its reason;
- a wake whose user entry holds its text as blocks counts as a wake (``ledger``);
- ``[agents.models.claude-code]`` naming an effort for a role, or a model where no ladder
  answers, is what the launch resolves (``resolve_model``).

The other four are defensive: a binary or a working folder that ``Path.resolve`` cannot
resolve (a symlink loop), and a timestamp no ISO reader takes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import harness, ledger
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.agent_adapters.transcripts import PendingTool
from aisquare.core.config import AgentModelDefault, AgentModelSettings, load_config, save_config

AGENT = "claude-code"
STAMP = "2026-11-05T17:00:00.000Z"
HOOK = "/x/.venv/bin/python -P -m aisquare hook stop"


def _reader() -> Any:
    reader = get_adapter(AGENT).transcript_reader
    assert reader is not None
    return reader


def _write(path: Path, *entries: dict[str, Any] | str) -> Path:
    lines = [e if isinstance(e, str) else json.dumps(e) for e in entries]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _user(text: Any, **extra: Any) -> dict[str, Any]:
    return {
        "type": "user",
        "isSidechain": False,
        "timestamp": STAMP,
        "uuid": f"u-{abs(hash(str(text)))}",
        "message": {"role": "user", "content": text},
        **extra,
    }


def _assistant(text: str, mid: str = "msg_1") -> dict[str, Any]:
    return {
        "type": "assistant",
        "isSidechain": False,
        "timestamp": STAMP,
        "uuid": f"a-{mid}",
        "message": {
            "id": mid,
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 10,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    }


@pytest.mark.parametrize(
    ("name", "given", "mark"),
    [
        ("Write", {"file_path": "/repo/src/app.py"}, "app.py"),
        ("Edit", {"file_path": "C:\\repo\\src\\main.rs"}, "main.rs"),
        ("NotebookEdit", {"notebook_path": "/repo/analysis.ipynb"}, "analysis.ipynb"),
    ],
)
def test_a_pending_file_edit_is_marked_by_its_files_name(
    name: str, given: dict[str, Any], mark: str
) -> None:
    pending = PendingTool("toolu_1", name, f"{name}(…)", given, None)
    assert _reader().call_mark(pending) == mark


def test_a_line_claude_code_is_still_writing_is_read_past(tmp_path: Path) -> None:
    transcript = _write(
        tmp_path / "s.jsonl",
        _user("say it"),
        _assistant("the agent's last words"),
        '{"type": "user", "message": {"role": "user", "content": "not yet wh',  # mid-write
    )
    reader = _reader()
    assert reader.reply_since(transcript, None) == "the agent's last words"
    facts, offset = reader.facts(transcript, 0)
    assert [fact.kind for fact in facts] == ["p"], facts
    assert offset <= transcript.stat().st_size


@pytest.mark.parametrize(
    ("content", "injected"),
    [
        (json.dumps({"decision": "block", "reason": "teammates spoke"}), len("teammates spoke")),
        ("a hook of the user's own that prints plain words", 0),
    ],
)
def test_a_stop_hooks_output_counts_only_as_the_reason_it_feeds_back(
    tmp_path: Path, content: str, injected: int
) -> None:
    stop = {
        "type": "attachment",
        "timestamp": STAMP,
        "uuid": "h-1",
        "attachment": {
            "type": "hook_success",
            "hookEvent": "Stop",
            "hookName": "Stop",
            "command": HOOK,
            "content": content,
        },
    }
    transcript = _write(tmp_path / "s.jsonl", _user("go"), _assistant("done"), stop)
    scan = _reader().usage(transcript, None)
    assert scan is not None and scan.delta.requests == 1
    assert scan.delta.injected_chars == injected


def test_a_wake_written_as_text_blocks_is_counted(tmp_path: Path) -> None:
    wake = _user([{"type": "text", "text": f"3 {ledger.WAKE_MARKER}: read them"}], isMeta=True)
    transcript = _write(
        tmp_path / "s.jsonl",
        _user("start"),
        _assistant("first", "msg_1"),
        wake,
        _assistant("after the wake", "msg_2"),
    )
    counted = _reader().ledger(transcript, since=None, until=None)
    assert counted.wakes == 1 and counted.requests == 2, counted


def _configure(model: str | None = None, effort: str | None = None) -> None:
    config = load_config()
    config.agents.models[AGENT] = AgentModelSettings(
        roles={"coder": AgentModelDefault(model=model, effort=effort)}
    )
    save_config(config)


def test_a_configured_effort_is_the_launchs_unless_one_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(effort="high")
    adapter = get_adapter(AGENT)
    resolved = adapter.resolve_model(
        "coder", binary="claude", env={}, probe=False, refresh=False, effort=None
    )
    assert resolved is not None and resolved.effort == "high"
    named = adapter.resolve_model(
        "coder", binary="claude", env={}, probe=False, refresh=False, effort="low"
    )
    assert named is not None and named.effort == "low", "an explicit --effort wins"


def test_a_configured_model_is_the_launchs_where_no_ladder_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(model="claude-sonnet-4-5")
    monkeypatch.setattr(harness, "resolve_model", lambda *args, **kwargs: None)
    resolved = get_adapter(AGENT).resolve_model(
        "coder", binary="claude", env={}, probe=False, refresh=False, effort=None
    )
    assert resolved is not None
    assert (resolved.model, resolved.source) == ("claude-sonnet-4-5", "configured")
    assert resolved.effort, "the role's own effort, as the ladder would give it"
