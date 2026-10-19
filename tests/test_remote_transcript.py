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
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from aisquare.core.paths import remote_audit_path, remote_state_path
from aisquare.services import transcript as transcript_service
from aisquare.services.remote_server import (
    NoSuchAgent,
    RequestError,
    Runtime,
    Sources,
    _limit_param,
    build_app,
)
from aisquare.services.remote_server import _live_transcript as live_transcript
from aisquare.services.transcript import EMPTY, SCAN_BUDGET, Page, read_page
from tests.remote_kit_helpers import make_client

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


def test_a_turns_time_goes_beside_its_lines_as_utc_never_as_the_machines_clock(
    tmp_path: Path,
) -> None:
    """r3 #9: the speaker's line ended in ``astimezone()``'s HH:MM, the machine's zone: a
    fleet on a UTC box read from a phone in UTC-7 said ``> you 17:05`` for a prompt typed
    at 10:05 by the phone's clock. The time goes beside the lines, as UTC, by the turn's
    first line, and the page tells it in the phone's own zone. A time written without a
    zone is UTC, as the tail reads it; the renderer read it as the machine's own. One
    written with another offset goes out as UTC too."""
    unzoned = _user("naive", uuid="n")
    unzoned["timestamp"] = "2026-09-12T17:07:00"
    path = _write(
        tmp_path / "times.jsonl",
        [
            _user("hello", uuid="u", stamp="2026-09-12T17:05:00.000Z"),
            _assistant({"type": "text", "text": "hi"}, uuid="a"),
            unzoned,
            _user("offset", uuid="o", stamp="2026-09-12T19:08:00+02:00"),
        ],
    )

    page = read_page(path)

    assert plain(page.lines) == [
        *("> you", "  hello", ""),
        *("* claude", "  hi", ""),
        *("> you", "  naive", ""),
        *("> you", "  offset", ""),
    ]
    assert page.stamps == {
        0: "2026-09-12T17:05:00+00:00",
        3: "2026-09-12T07:54:39+00:00",
        6: "2026-09-12T17:07:00+00:00",
        9: "2026-09-12T17:08:00+00:00",
    }
    assert page.page_json()["stamps"] == {str(at): stamp for at, stamp in page.stamps.items()}


def test_a_turns_time_written_without_a_zone_is_utc_on_a_machine_in_another_zone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The test above, on a machine whose zone is not UTC, where the two readings differ:
    on CI's runners, which are UTC, the machine's own zone reads 17:07 as 17:07 UTC too, so
    a renderer that took a time without a zone as local time passed there. In UTC-7 it sent
    00:07 the next day. Skipped where ``time`` has no ``tzset`` (Windows): the zone cannot
    be switched for the process there, as ``test_reset_formatter.py`` says."""
    # In the body and on `sys.platform`, as test_usage_aware_accounts.py has it: mypy's run
    # on the Windows leg then narrows past the skip and never sees `time.tzset` missing.
    if sys.platform == "win32":
        pytest.skip("time.tzset is POSIX-only: the process zone cannot be switched for the test")
    unzoned = _user("naive", uuid="n")
    unzoned["timestamp"] = "2026-09-12T17:07:00"
    path = _write(tmp_path / "unzoned.jsonl", [unzoned])
    with monkeypatch.context() as local:
        local.setenv("TZ", "America/Los_Angeles")
        time.tzset()
        try:
            page = read_page(path)
        finally:
            local.undo()
            time.tzset()
    assert page.stamps == {0: "2026-09-12T17:07:00+00:00"}


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


def test_a_compaction_summary_is_one_line_saying_so_never_the_persons_words(
    tmp_path: Path,
) -> None:
    """Claude Code writes a compaction's summary as a "user" record with a string content and
    no ``isMeta``: kilobytes the model wrote about the conversation so far, which rendered
    under ``> you`` as words the human never wrote. A partial compaction ("summarize up to
    here") marks its summary the same way, without ``isVisibleInTranscriptOnly``."""
    opening = (
        "This session is being continued from a previous conversation that ran out of "
        "context. The summary below covers the earlier portion of the conversation."
    )
    summary = {
        **_user(opening + "\n\n" + "Analysis: the cache work so far.\n" * 200, uuid="s1"),
        "isCompactSummary": True,
        "isVisibleInTranscriptOnly": True,
    }
    partial = {**_user("Summary of the messages before this point.", uuid="s2")}
    partial["isCompactSummary"] = True
    boundary = {
        "type": "system",
        "subtype": "compact_boundary",
        "content": "Conversation compacted",
    }
    path = _write(
        tmp_path / "compacted.jsonl",
        [
            _user("fix the cache", uuid="u1"),
            _assistant({"type": "text", "text": "On it."}, uuid="a1"),
            boundary,
            summary,
            _assistant({"type": "text", "text": "Picking up the cache work."}, uuid="a2"),
            partial,
            _user("ship it", uuid="u2"),
        ],
    )
    text = plain(read_page(path).lines)
    assert sum(line.startswith("> you") for line in text) == 2, "fix the cache, ship it"
    assert not any("being continued" in line or "Summary of the" in line for line in text)
    assert text.count("  ⎿ conversation compacted") == 2
    assert "  Picking up the cache work." in text


def test_claude_codes_own_records_are_never_the_persons_words(tmp_path: Path) -> None:
    """Claude Code writes a "user" record of its own, with no ``isMeta``, when a background
    task ends (``origin`` ``task-notification``, ``promptSource`` "system", its notice and
    often a sub-agent's whole report as the text) and for the output of a slash command
    (``<local-command-stdout>``). Each rendered under ``> you``, the human's words: on this
    machine 812 notices, one of them 1 453 lines. A notice is one dim line, its summary (or
    its first line, in no tag); an output one dim line, its first; a reminder nothing."""
    notice = {
        **_user(
            "<task-notification>\n<task-id>bq1</task-id>\n<tool-use-id>toolu_1</tool-use-id>\n"
            "<output-file>/tmp/bq1.output</output-file>\n<status>completed</status>\n"
            '<summary>Background command "make build" completed (exit code 0)</summary>\n'
            "<result>" + "the sub-agent's report, line after line\n" * 300 + "</result>\n"
            "</task-notification>",
            uuid="n1",
        ),
        "origin": {"kind": "task-notification"},
        "promptSource": "system",
    }
    untagged = {
        **_user("A background task you started has finished.", uuid="n2"),
        "origin": {"kind": "task-notification"},
    }
    path = _write(
        tmp_path / "own.jsonl",
        [
            _user("build it in the background", uuid="u1"),
            notice,
            untagged,
            _user("<local-command-stdout>Set model to Opus</local-command-stdout>", uuid="o1"),
            _user("<local-command-stdout></local-command-stdout>", uuid="o2"),
            _user("<system-reminder>The date changed.</system-reminder>", uuid="r1"),
            _user("ship it", uuid="u2"),
        ],
    )
    text = plain(read_page(path, width=80).lines)
    assert text == [
        "> you",
        "  build it in the background",
        "",
        '  ⎿ Background command "make build" completed (exit code 0)',
        "",
        "  ⎿ A background task you started has finished.",
        "",
        "  ⎿ Set model to Opus",
        "",
        "> you",
        "  ship it",
        "",
    ]


def test_the_persons_own_commands_are_theirs_without_the_tags(tmp_path: Path) -> None:
    """A slash command and a ``!`` command are the human's, recorded in Claude Code's tags:
    ``> you``, the command as typed. Words a person typed that only start like a tag, never
    closing it, are theirs as typed."""
    path = _write(
        tmp_path / "commands.jsonl",
        [
            _user(
                "<command-message>model</command-message>\n<command-name>/model</command-name>\n"
                "<command-args>opus</command-args>",
                uuid="c1",
            ),
            _user("<bash-input>git status</bash-input>", uuid="b1"),
            _user(
                "<bash-stdout>On branch main</bash-stdout><bash-stderr></bash-stderr>", uuid="b2"
            ),
            _user("<command-name> is the tag the parser misses: fix it", uuid="u1"),
        ],
    )
    assert plain(read_page(path, width=80).lines) == [
        "> you",
        "  /model opus",
        "",
        "> you",
        "  ! git status",
        "",
        "  ⎿ On branch main",
        "",
        "> you",
        "  <command-name> is the tag the parser misses: fix it",
        "",
    ]


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


JAPANESE = (
    "ページが文字起こしをスマートフォンの幅に合わせて折り返すかどうかを確認しました。"
    "サーバーは各行を端末の列で数えます。"
)


@pytest.mark.parametrize(
    "said",
    [JAPANESE, "构建完成 🎉 所有测试都通过了 " * 6, "done ✅ " * 40, JAPANESE + " word" * 30],
)
def test_wide_characters_are_wrapped_by_the_columns_they_take(tmp_path: Path, said: str) -> None:
    """The page asks for the width in columns (``?width=``), and a line of Japanese wrapped by
    characters at 40 took 78 of them: the phone wrapped each line again, into a full row and
    a ragged half outside the indent (review of #243, sweep 5). A CJK character or an emoji
    takes two columns, and the words come back whole."""
    from rich.cells import cell_len

    path = _write(tmp_path / "wide.jsonl", [_user(said, uuid="u")])
    for width in (40, 44, 53):
        speaker, *body, blank = plain(read_page(path, width=width).lines)
        assert (speaker, blank) == ("> you", "")
        assert all(cell_len(line) <= width and line.startswith("  ") for line in body), [
            (cell_len(line), line) for line in body if cell_len(line) > width
        ]
        if " " not in said:  # no word to keep whole: each line but the last fills its row
            assert all(cell_len(line) >= width - 1 for line in body[:-1])
        assert "".join(line[2:] for line in body).replace(" ", "") == said.replace(" ", "")


def test_a_tool_call_or_a_note_is_one_row_however_wide_its_words(tmp_path: Path) -> None:
    """A tool call's summary went up to 82 columns, whatever width the page asked for, and a
    note was cut by characters: either took two of a phone's rows, the second outside the
    ``⎿`` (review of #243, sweep 5). Each fits the width, cut where it ends."""
    from rich.cells import cell_len

    command = "git commit -m 'リリース前にキャッシュの無効化を直す' && " + "x" * 60
    notice = {
        **_user("バックグラウンドのビルドが終わりました。" * 4, uuid="n"),
        "origin": {"kind": "task-notification"},
    }
    call = {"type": "tool_use", "name": "Bash", "input": {"command": command}}
    path = _write(tmp_path / "calls.jsonl", [_assistant(call, uuid="a"), notice])
    for width in (40, 44, 80):
        notes = [line for line in plain(read_page(path, width=width).lines) if "⎿" in line]
        call_line, note_line = notes
        assert call_line.startswith("  ⎿ Bash(git commit") and note_line.startswith("  ⎿ バック")
        assert all(cell_len(line) <= width and line.endswith("…") for line in notes), notes
    short = plain(read_page(path, width=200).lines)
    assert f"  ⎿ Bash({command[:72]})" in short, "the summary as it was, where it fits"


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


def _page_to_the_start(path: Path) -> list[Page]:
    """Every page from the newest back, failing on a page that cannot be followed."""
    pages: list[Page] = []
    cursor: str | None = None
    for _ in range(50):
        page = read_page(path, limit=200, before=cursor)
        assert not (page.more and page.cursor is None), f"more, and nowhere to go: {page}"
        pages.append(page)
        if not page.more:
            return pages
        assert page.cursor is not None
        assert cursor is None or int(page.cursor) < int(cursor), "each page reads further back"
        cursor = page.cursor
    raise AssertionError("paging never reached the start of the file")


def test_paging_reaches_the_start_past_a_paste_longer_than_the_scan_budget(
    tmp_path: Path,
) -> None:
    """A page whose budget runs out inside one line still hands back a cursor that moves.

    Review of #243, round 1: a user line, a 3 MB image, an assistant line. The
    cursor was the oldest RENDERED turn, so the second page read the same 2 MB
    of image again, rendered nothing, and answered ``more`` with no cursor at
    all: the first message could never be reached.
    """
    image = {
        "type": "user",
        "uuid": "img",
        "message": {"role": "user", "content": [{"type": "image", "data": "i" * 3_000_000}]},
    }
    path = _write(
        tmp_path / "image.jsonl",
        [
            _user("the first question", uuid="u"),
            image,
            _assistant({"type": "text", "text": "the last answer"}, uuid="a"),
        ],
    )
    pages = _page_to_the_start(path)
    text = "\n".join(plain([line for page in reversed(pages) for line in page.lines]))
    assert text.index("the first question") < text.index("the last answer")


def test_paging_reaches_the_start_past_more_unrendered_lines_than_the_budget(
    huge: Path,
) -> None:
    """The same dead end, made of many lines that render nothing (18 MB of attachments)."""
    pages = _page_to_the_start(huge)
    text = "\n".join(plain([line for page in reversed(pages) for line in page.lines]))
    assert "question 0" in text and "answer 29" in text
    assert all(not page.lines for page in pages[1:]), "the turns were all on the first page"


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
    client = make_client(build_app(runtime, sources=_sources(transcript), dist_dir=tmp_path))
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
    assert response.json()["error"] == "no_such_agent", "the page says the agent is gone"


def test_the_transcript_is_behind_the_same_gates(runtime: Runtime, tmp_path: Path) -> None:
    """A conversation is the most private thing this server serves."""
    app = build_app(runtime, sources=_sources(_echo), dist_dir=tmp_path)
    anonymous = make_client(app)
    assert anonymous.get(f"/r/{runtime.token}/api/transcript/coder-1").status_code == 401
    assert anonymous.get("/r/wrong/api/transcript/coder-1").status_code == 404
    anonymous.cookies.set("asq_remote", "forged")
    assert anonymous.get(f"/r/{runtime.token}/api/transcript/coder-1").status_code == 401


def test_reading_a_transcript_is_not_a_write(runtime: Runtime, tmp_path: Path) -> None:
    """§4-M: a read, so it works with allow_write off and leaves no audit line."""
    client = _client(runtime, _echo, tmp_path)
    assert runtime.allow_write is False
    assert client.get(f"/r/{runtime.token}/api/transcript/coder-1").status_code == 200
    endpoints = [line.split(" ")[2] for line in remote_audit_path().read_text().splitlines()]
    assert endpoints == ["unlock"], "the unlock is on the trail; the read is not"


def test_limit_param_parsing() -> None:
    assert _limit_param(None) == 0 and _limit_param("") == 0
    assert _limit_param("50") == 50
    with pytest.raises(ValueError, match="whole number"):
        _limit_param("many")
    with pytest.raises(ValueError, match="negative"):
        _limit_param("-2")


# --- a cursor is a place in one conversation ------------------------------------------------


def _two_conversations(tmp_path: Path) -> None:
    """coder-1 on session ``ses_1``, a long conversation, and ``ses_2``, the one a ``/clear``
    starts: the store as the team's hooks leave it, the row still on ``ses_1``."""
    from datetime import UTC, datetime, timedelta

    from aisquare.core.store import store_session
    from aisquare.models import FleetAgent, ProjectInfo, TeamSession

    root = tmp_path / "alpha"
    old = _write(tmp_path / "old.jsonl", [_user(f"OLD {n}", uuid=f"o{n}") for n in range(40)])
    new = _write(tmp_path / "new.jsonl", [_user(f"NEW {n}", uuid=f"n{n}") for n in range(3)])
    born = datetime.now(UTC) - timedelta(hours=1)
    with store_session() as store:
        project = store.onboard_project(ProjectInfo(id="prj_alpha", root=root))
        for session_id, path in (("ses_1", old), ("ses_2", new)):
            store.upsert_session(
                TeamSession(
                    id=session_id, project_id=project.id, role="coder", label="coder-1",
                    started_at=born, last_seen_at=born, transcript_path=str(path),
                )
            )  # fmt: skip
        store.upsert_fleet_agent(
            FleetAgent(
                id="agt_1", project_id=project.id, label="coder-1", role="coder", pane_id="%1",
                session_id="ses_1", cwd=root, created_at=born,
            )
        )  # fmt: skip


def _page_lines(payload: dict[str, object]) -> list[str]:
    lines = payload["lines"]
    assert isinstance(lines, list)
    return plain(lines)


def test_a_cursor_reads_on_only_in_the_conversation_it_came_from(
    runtime: Runtime, tmp_path: Path
) -> None:
    """The cursor was a bare byte offset, read in whatever file the label's session names
    now. After a ``/clear`` (the row moves to the new session), a fresh restart or a new
    agent under a freed label, Load older read the new conversation from there, and the
    page put it above the old one as its past. It names its conversation: another's, or a
    bare offset, is a 409 ``stale_cursor``, and the page reads the new one from its end."""
    from datetime import UTC, datetime, timedelta

    from aisquare.core.store import store_session

    _two_conversations(tmp_path)
    first = live_transcript("coder-1", "prj_alpha", 10, None, 60)
    cursor = first["cursor"]
    assert isinstance(cursor, str) and cursor.startswith("ses_1:") and first["more"] is True
    assert "  OLD 29" in _page_lines(live_transcript("coder-1", "prj_alpha", 10, cursor, 60))
    with store_session() as store:
        lease = datetime.now(UTC) + timedelta(minutes=30)
        assert store.adopt_fleet_agent_session("agt_1", "ses_1", "ses_2", lease)  # a /clear
    for stale in (cursor, cursor.split(":")[1], "ses_2:nonsense", "ses_2:0", "ses_2:\u00b2"):
        with pytest.raises(RequestError) as refused:
            live_transcript("coder-1", "prj_alpha", 10, stale, 60)
        assert (refused.value.status, refused.value.error) == (409, "stale_cursor"), stale
    assert _page_lines(live_transcript("coder-1", "prj_alpha", 10, None, 60))[1] == "  NEW 0"
    client = _client(runtime, live_transcript, tmp_path)
    response = client.get(
        f"/r/{runtime.token}/api/transcript/coder-1",
        params={"project": "prj_alpha", "before": cursor, "width": 60},
    )
    assert response.status_code == 409 and response.json()["error"] == "stale_cursor"


def test_a_cursor_whose_offset_no_file_has_is_stale_however_long_it_is(
    runtime: Runtime, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``int`` raises past 4 300 digits, so an offset that long in a cursor naming the agent's
    own conversation was a 503 ``unavailable`` with Python's message, and a warning in the
    log for each request (sweep 5 of #243). No file has such an offset: it is a 409
    ``stale_cursor``, as every other cursor the server did not write is."""
    _two_conversations(tmp_path)
    for digits in (20, 4_301, 5_000):
        with pytest.raises(RequestError) as refused:
            live_transcript("coder-1", "prj_alpha", 10, "ses_1:" + "9" * digits, 60)
        assert (refused.value.status, refused.value.error) == (409, "stale_cursor"), digits
    client = _client(runtime, live_transcript, tmp_path)
    with caplog.at_level("WARNING", logger="aisquare.services.remote_server"):
        response = client.get(
            f"/r/{runtime.token}/api/transcript/coder-1",
            params={"project": "prj_alpha", "before": "ses_1:" + "9" * 5_000, "width": 60},
        )
    assert response.status_code == 409 and response.json()["error"] == "stale_cursor"
    assert not [record for record in caplog.records if record.levelname == "WARNING"]
