"""The agent's conversation on the wire (PLAN §4-M).

The pane cannot answer "what did this agent already say" — agent panes are
alternate-screen and tmux keeps no scrollback for them (§4-L, measured: every
live agent reports ``history_size: 0``). The conversation is the JSONL
transcript the board records, and these pin the reading, the rendering, the
paging and the fact that a 18 MB file is seeked rather than slurped.

The fixtures write REAL-shaped Claude Code JSONL — the record types and content
blocks taken from an actual transcript on this machine — rather than mocking the
reader, so the parsing is tested against the format it will really meet, while
staying hermetic enough to run anywhere.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from aisquare.core.paths import remote_audit_path, remote_state_path
from aisquare.services import transcript as transcript_service
from aisquare.services.remote_server import (
    NoSuchAgent,
    Runtime,
    Sources,
    _limit_param,
    build_app,
)
from aisquare.services.transcript import EMPTY, SCAN_BUDGET, Page, read_page

PASSWORD = "Test1234"


# --- writing a transcript that looks like the real thing -------------------------------


def _user(text: str, *, uuid: str, stamp: str = "2026-09-12T07:54:34.000Z") -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": stamp,
        "message": {"role": "user", "content": text},
    }


def _assistant(*blocks: dict[str, Any], uuid: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": "2026-09-12T07:54:39.000Z",
        "message": {"role": "assistant", "content": list(blocks)},
    }


def _tool_result(output: str, *, uuid: str) -> dict[str, Any]:
    """A "user" record that is plumbing, not a person — must never be labelled one."""
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": "2026-09-12T07:54:43.000Z",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "content": output}],
        },
    }


def _write(path: Path, records: list[dict[str, Any]]) -> Path:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


@pytest.fixture
def conversation(tmp_path: Path) -> Path:
    """Twelve exchanges, plus the metadata and plumbing records a real file carries."""
    records: list[dict[str, Any]] = [
        {"type": "custom-title", "title": "ignored"},
        {"type": "mode", "mode": "ignored"},
    ]
    for turn in range(12):
        records.append(_user(f"question {turn}", uuid=f"u{turn}"))
        records.append(_assistant({"type": "thinking", "thinking": "quiet part"}, uuid=f"t{turn}"))
        records.append(_assistant({"type": "text", "text": f"answer {turn}"}, uuid=f"a{turn}"))
        records.append(
            _assistant(
                {"type": "tool_use", "name": "Bash", "input": {"command": f"echo {turn}"}},
                uuid=f"x{turn}",
            )
        )
        records.append(_tool_result(f"output {turn}", uuid=f"r{turn}"))
    return _write(tmp_path / "conversation.jsonl", records)


def plain(lines: list[str]) -> list[str]:
    import re

    return [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in lines]


# --- what is rendered, and what is deliberately not -------------------------------------


def test_a_page_is_the_conversation_oldest_line_first(conversation: Path) -> None:
    page = read_page(conversation, limit=200)
    text = plain(page.lines)
    assert any("question 0" in line for line in text)
    assert any("answer 11" in line for line in text)
    first = next(i for i, line in enumerate(text) if "question 0" in line)
    last = next(i for i, line in enumerate(text) if "answer 11" in line)
    assert first < last, "§4-M: newest last"


def test_each_speaker_is_named(conversation: Path) -> None:
    text = plain(read_page(conversation, limit=200).lines)
    assert any(line.startswith("> you") for line in text)
    assert any(line.startswith("* claude") for line in text)


def test_a_tool_call_is_one_summary_line(conversation: Path) -> None:
    text = plain(read_page(conversation, limit=200).lines)
    calls = [line for line in text if "Bash(" in line]
    assert calls, "a transcript without tool calls reads as a monologue with gaps"
    assert all(line.strip().startswith("⎿") for line in calls)
    assert any("echo 3" in line for line in calls)


def test_tool_results_and_thinking_are_not_rendered(conversation: Path) -> None:
    """They are the bulk of the megabytes and are not the agent's responses."""
    text = "\n".join(plain(read_page(conversation, limit=200).lines))
    assert "output 3" not in text
    assert "quiet part" not in text


def test_a_tool_result_record_is_never_labelled_as_the_person(tmp_path: Path) -> None:
    """It is typed "user" in the file but a human did not say it."""
    path = _write(tmp_path / "plumbing.jsonl", [_tool_result("ls output", uuid="r")])
    page = read_page(path)
    assert page.lines == []


def test_metadata_records_are_skipped(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "meta.jsonl",
        [{"type": t, "value": 1} for t in ("custom-title", "mode", "attachment", "system")],
    )
    assert read_page(path).lines == []


def test_long_text_is_wrapped_to_the_width_it_will_land_in(tmp_path: Path) -> None:
    path = _write(tmp_path / "wide.jsonl", [_user("word " * 200, uuid="u")])
    narrow = plain(read_page(path, width=40).lines)
    wide = plain(read_page(path, width=120).lines)
    assert all(len(line) <= 40 for line in narrow)
    assert len(narrow) > len(wide), "a narrow pane needs more lines for the same words"


# --- paging backwards -------------------------------------------------------------------


def test_more_is_false_and_cursor_none_when_the_whole_file_fits(conversation: Path) -> None:
    page = read_page(conversation, limit=1000)
    assert page.more is False
    assert page.cursor is None


def test_a_short_limit_reports_more_and_hands_back_a_cursor(conversation: Path) -> None:
    page = read_page(conversation, limit=3)
    assert page.more is True
    assert page.cursor is not None and int(page.cursor) > 0


def test_paging_backwards_neither_duplicates_nor_drops_a_turn(conversation: Path) -> None:
    """The seam is the whole risk: stitched pages must equal one big page."""
    whole = read_page(conversation, limit=1000)
    pages: list[Page] = []
    cursor: str | None = None
    for _ in range(50):
        page = read_page(conversation, limit=3, before=cursor)
        pages.append(page)
        if not page.more or page.cursor is None:
            break
        cursor = page.cursor
    assert pages[-1].more is False, "paging terminates at the start of the file"
    stitched = [line for page in reversed(pages) for line in page.lines]
    assert stitched == whole.lines


def test_the_newest_page_comes_first_when_paging(conversation: Path) -> None:
    newest = plain(read_page(conversation, limit=2).lines)
    older = plain(
        read_page(conversation, limit=2, before=read_page(conversation, limit=2).cursor).lines
    )
    assert any("answer 11" in line for line in newest)
    assert not any("answer 11" in line for line in older)


def test_a_nonsense_cursor_reads_as_the_end_rather_than_failing(conversation: Path) -> None:
    assert read_page(conversation, before="not-a-number").lines == read_page(conversation).lines
    assert read_page(conversation, before="0").lines == read_page(conversation).lines


# --- a huge file is seeked, not slurped ---------------------------------------------------


class _CountingHandle:
    """A file handle that remembers how many bytes were actually read through it."""

    def __init__(self, inner: Any, tally: list[int]) -> None:
        self._inner = inner
        self._tally = tally

    def read(self, size: int = -1) -> bytes:
        data: bytes = self._inner.read(size)
        self._tally.append(len(data))
        return data

    def seek(self, *args: Any, **kwargs: Any) -> int:
        return int(self._inner.seek(*args, **kwargs))

    def __enter__(self) -> _CountingHandle:
        self._inner.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self._inner.__exit__(*exc)


@pytest.fixture
def huge(tmp_path: Path) -> Path:
    """~18 MB: a wall of pasted attachments, then the recent conversation."""
    path = tmp_path / "huge.jsonl"
    filler = {"type": "attachment", "uuid": "big", "content": "x" * 2_000_000}
    with path.open("w", encoding="utf-8") as handle:
        for _ in range(9):
            handle.write(json.dumps(filler) + "\n")
        for turn in range(30):
            handle.write(json.dumps(_user(f"question {turn}", uuid=f"u{turn}")) + "\n")
            handle.write(
                json.dumps(_assistant({"type": "text", "text": f"answer {turn}"}, uuid=f"a{turn}"))
                + "\n"
            )
    assert path.stat().st_size > 17_000_000
    return path


def test_a_huge_transcript_is_seeked_not_read_whole(
    huge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tally: list[int] = []
    original = Path.open

    def counting(self: Path, *args: Any, **kwargs: Any) -> Any:
        handle = original(self, *args, **kwargs)
        return _CountingHandle(handle, tally) if self == huge else handle

    monkeypatch.setattr(Path, "open", counting)
    page = read_page(huge, limit=20)
    read = sum(tally)
    assert page.lines, "the recent conversation still comes back"
    assert read < 2_000_000, f"read {read} bytes of an 18MB file"
    assert read < huge.stat().st_size / 8


def test_the_scan_budget_stops_a_wall_of_paste_and_says_more(huge: Path) -> None:
    """Asking for more turns than the tail holds returns what it found, honestly."""
    page = read_page(huge, limit=1000)
    assert page.more is True, "there is more file behind the wall of attachments"
    assert page.cursor is not None
    assert len(page.lines) > 0


def test_an_enormous_single_line_is_skipped_without_parsing_it(tmp_path: Path) -> None:
    path = tmp_path / "paste.jsonl"
    monster = {"type": "user", "uuid": "m", "message": {"role": "user", "content": "y" * 500_000}}
    _write(path, [_user("small one", uuid="s"), monster])
    text = "\n".join(plain(read_page(path).lines))
    assert "small one" in text
    assert "yyyy" not in text, "over MAX_LINE, so never parsed or rendered"


# --- a transcript that is not there --------------------------------------------------------


def test_a_missing_transcript_is_empty_not_an_error(tmp_path: Path) -> None:
    assert read_page(tmp_path / "nope.jsonl") == EMPTY
    assert read_page(None) == EMPTY


def test_an_empty_or_unreadable_transcript_is_empty(tmp_path: Path) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert read_page(empty) == EMPTY
    assert read_page(tmp_path) == EMPTY, "a directory is unreadable, not a 500"


def test_garbage_lines_do_not_sink_the_page(tmp_path: Path) -> None:
    path = tmp_path / "torn.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        handle.write("{not json at all\n")
        handle.write(json.dumps(_user("still here", uuid="u")) + "\n")
        handle.write('{"type": "user", "message": "not an object"}\n')
    assert any("still here" in line for line in plain(read_page(path).lines))


def test_scan_budget_and_limits_are_the_pinned_numbers() -> None:
    assert transcript_service.DEFAULT_LIMIT == 200
    assert transcript_service.LIMIT_CAP >= 200
    assert SCAN_BUDGET <= 4_000_000


def test_the_limit_is_capped(conversation: Path) -> None:
    assert read_page(conversation, limit=10**9).lines == read_page(conversation, limit=1000).lines


# --- the endpoint ----------------------------------------------------------------------------


@pytest.fixture
def runtime(isolated_home: Path) -> Runtime:
    rt = Runtime(remote_state_path(), remote_audit_path())
    rt._state.password = PASSWORD
    rt._save_state()
    return rt


def _sources(transcript: Any) -> Sources:
    return Sources(
        projects=lambda: [],
        fleet=lambda project: {},
        board=lambda project: {},
        tasks=lambda project: [],
        memory=lambda project: [],
        panes=lambda agent, project, history: {"rows": [], "width": 0, "height": 0},
        transcript=transcript,
        explainability=lambda agent, project: {"available": False},
    )


def _echo(
    agent: str, project: str | None, limit: int, before: str | None, width: int | None
) -> dict[str, object]:
    return {"lines": [f"{agent}/{project}/{limit}/{before}/{width}"], "cursor": None, "more": False}


def _client(runtime: Runtime, transcript: Any, tmp_path: Path) -> TestClient:
    client = TestClient(build_app(runtime, sources=_sources(transcript), dist_dir=tmp_path))
    assert (
        client.post(f"/r/{runtime.token}/api/unlock", json={"password": PASSWORD}).status_code
        == 200
    )
    return client


def test_the_route_forwards_project_limit_before_and_width(
    runtime: Runtime, tmp_path: Path
) -> None:
    client = _client(runtime, _echo, tmp_path)
    base = f"/r/{runtime.token}/api/transcript/coder-1"
    assert client.get(base).json()["lines"] == ["coder-1/None/0/None/None"]
    scoped = client.get(
        base, params={"project": "prj_x", "limit": 50, "before": "1234", "width": 42}
    )
    assert scoped.json()["lines"] == ["coder-1/prj_x/50/1234/42"]


def test_the_page_shape_is_the_contract(runtime: Runtime, tmp_path: Path) -> None:
    client = _client(runtime, _echo, tmp_path)
    body = client.get(f"/r/{runtime.token}/api/transcript/coder-1").json()
    assert set(body) == {"lines", "cursor", "more"}


def test_a_bad_limit_is_a_400(runtime: Runtime, tmp_path: Path) -> None:
    client = _client(runtime, _echo, tmp_path)
    base = f"/r/{runtime.token}/api/transcript/coder-1"
    for raw in ("lots", "-1"):
        assert client.get(base, params={"limit": raw}).status_code == 400, raw


def test_an_unknown_agent_is_404(runtime: Runtime, tmp_path: Path) -> None:
    def missing(
        agent: str, project: str | None, limit: int, before: str | None, width: int | None
    ) -> Any:
        raise NoSuchAgent(f"no live agent {agent!r}")

    client = _client(runtime, missing, tmp_path)
    response = client.get(f"/r/{runtime.token}/api/transcript/ghost")
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_the_transcript_is_behind_the_same_gates(runtime: Runtime, tmp_path: Path) -> None:
    """A conversation is the most private thing this server serves."""
    app = build_app(runtime, sources=_sources(_echo), dist_dir=tmp_path)
    anonymous = TestClient(app)
    assert anonymous.get(f"/r/{runtime.token}/api/transcript/coder-1").status_code == 401
    assert anonymous.get("/r/wrong/api/transcript/coder-1").status_code == 404
    anonymous.cookies.set("asq_remote", "forged")
    assert anonymous.get(f"/r/{runtime.token}/api/transcript/coder-1").status_code == 401


def test_reading_a_transcript_is_not_a_write(runtime: Runtime, tmp_path: Path) -> None:
    """§4-M: a read, so it works with allow_write off and leaves no audit line."""
    client = _client(runtime, _echo, tmp_path)
    assert runtime.allow_write is False
    assert client.get(f"/r/{runtime.token}/api/transcript/coder-1").status_code == 200
    assert not remote_audit_path().exists()


def test_limit_param_parsing() -> None:
    assert _limit_param(None) == 0 and _limit_param("") == 0
    assert _limit_param("50") == 50
    with pytest.raises(ValueError, match="whole number"):
        _limit_param("many")
    with pytest.raises(ValueError, match="negative"):
        _limit_param("-2")
