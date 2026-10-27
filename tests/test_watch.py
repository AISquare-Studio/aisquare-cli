"""The live board: dependency readiness, feed rendering, and the TUI smoke."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.cli.watch import feed_line
from aisquare.services import team as team_service
from tests import toast_log
from tests.fake_clipboard import NEEDS_POSIX, TEXT, install
from tests.projects import add_project
from tests.ui_workers import settle_page


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    add_project(work)
    return work


def _add(runner: CliRunner, title: str, *args: str) -> str:
    out = runner.invoke(app, ["--json", "task", "add", title, *args])
    return str(json.loads(out.stdout)["id"])


def test_task_next_respects_dependencies(runner: CliRunner, work_dir: Path) -> None:
    team_service.activate()
    first = _add(runner, "build the API")
    second = _add(runner, "wire the UI", "--needs", first)
    # Only the prerequisite is ready; the dependent waits.
    ready = json.loads(runner.invoke(app, ["--json", "task", "next"]).stdout)
    assert ready["id"] == first
    runner.invoke(app, ["task", "claim", first])
    assert json.loads(runner.invoke(app, ["--json", "task", "next"]).stdout) is None
    # Resolving the prerequisite unlocks the dependent.
    runner.invoke(app, ["task", "done", first])
    unlocked = json.loads(runner.invoke(app, ["--json", "task", "next"]).stdout)
    assert unlocked["id"] == second
    # The board tells agents why a task is waiting.
    third = _add(runner, "ship it", "--needs", second)
    board = runner.invoke(app, ["board"]).stdout
    assert "⧗ waits on" in board and third  # dependent annotated


def test_task_add_rejects_unknown_dependency(runner: CliRunner, work_dir: Path) -> None:
    team_service.activate()
    result = runner.invoke(app, ["--json", "task", "add", "x", "--needs", "tsk_nope"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "not_found"


def test_feed_line_is_bot_styled(runner: CliRunner, work_dir: Path) -> None:
    team_service.activate()
    runner.invoke(app, ["note", "JWT it is", "--kind", "decision"])
    events = team_service.log_events()
    line = str(feed_line(events[-1], {}))
    assert "💡" in line and "decided" in line and "JWT it is" in line


def test_tui_smoke_click_task_shows_detail(runner: CliRunner, work_dir: Path) -> None:
    pytest.importorskip("textual", reason="the [tui] extra is not installed")
    from textual.widgets import DataTable, OptionList

    from aisquare.cli import watch as watch_mod

    team_service.activate()
    first = _add(runner, "build the API")
    _add(runner, "wire the UI", "--needs", first, "--detail", "hook Atlas chat to v3")
    runner.invoke(app, ["note", "kickoff", "--kind", "decision"])

    async def drive() -> tuple[int, int, str]:
        app_cls = watch_mod._build_app_class(interval=60.0)
        async with app_cls().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            table = pilot.app.query_one("#tasks", DataTable)
            feed = pilot.app.query_one("#feed", OptionList)
            table.focus()
            table.move_cursor(row=1)
            await pilot.press("enter")  # RowSelected shows the detail
            await pilot.pause()
            return table.row_count, feed.option_count, pilot.app.board.detail_text

    rows, options, detail = asyncio.run(drive())
    assert rows == 2
    assert options >= 3  # activate + task_added x2 + decision
    assert "wire the UI" in detail and "hook Atlas chat to v3" in detail
    assert "⧗" in detail  # unmet dependency marked


def test_theme_picker_stays_open_and_autosaves(
    runner: CliRunner, work_dir: Path, isolated_home: Path
) -> None:
    pytest.importorskip("textual", reason="the [tui] extra is not installed")
    from aisquare.cli import watch as watch_mod

    team_service.activate()

    async def browse() -> tuple[bool, str, str]:
        app_cls = watch_mod._build_app_class(interval=60.0)
        async with app_cls().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("t")  # open the picker
            await pilot.pause()
            await pilot.press("down")  # browsing applies instantly…
            await pilot.press("down")
            await pilot.pause()
            still_open = type(pilot.app.screen).__name__ == "ThemePicker"
            applied = str(pilot.app.theme)
            await pilot.press("escape")  # …until the explicit close
            await pilot.pause()
            closed = type(pilot.app.screen).__name__ != "ThemePicker"
            assert closed
            _on_file(pilot.app)
            return still_open, applied, str(pilot.app.theme)

    still_open, applied, final = asyncio.run(browse())
    assert still_open  # selection does NOT close the dialog
    assert applied == final  # the browsed theme stuck after closing
    saved = json.loads((isolated_home / "state.json").read_text(encoding="utf-8"))["board_theme"]
    assert saved == final  # …and was autosaved without any save step

    async def relaunch() -> str:
        app_cls = watch_mod._build_app_class(interval=60.0)
        async with app_cls().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            return str(pilot.app.theme)

    assert asyncio.run(relaunch()) == final  # restored on next launch


def test_screenshot_key_saves_svg_locally(
    runner: CliRunner, work_dir: Path, isolated_home: Path
) -> None:
    pytest.importorskip("textual", reason="the [tui] extra is not installed")
    from aisquare.cli import watch as watch_mod

    team_service.activate()

    async def snap() -> list[Path]:
        app_cls = watch_mod._build_app_class(interval=60.0)
        async with app_cls().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
        return list((isolated_home / "screenshots").glob("board-*.svg"))

    shots = asyncio.run(snap())
    assert len(shots) == 1
    assert "<svg" in shots[0].read_text(encoding="utf-8")[:200]


def test_palette_change_theme_opens_our_picker(
    runner: CliRunner, work_dir: Path, isolated_home: Path
) -> None:
    pytest.importorskip("textual", reason="the [tui] extra is not installed")
    from aisquare.cli import watch as watch_mod

    team_service.activate()

    async def via_action() -> str:
        app_cls = watch_mod._build_app_class(interval=60.0)
        async with app_cls().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # what the command palette's "Change theme" invokes:
            await pilot.app.run_action("app.change_theme")
            await pilot.pause()
            return type(pilot.app.screen).__name__

    assert asyncio.run(via_action()) == "ThemePicker"


def test_select_mode_freezes_feed_and_resumes_with_backlog(
    runner: CliRunner, work_dir: Path, isolated_home: Path
) -> None:
    pytest.importorskip("textual", reason="the [tui] extra is not installed")
    from textual.widgets import OptionList

    from aisquare.cli import watch as watch_mod

    team_service.activate()
    runner.invoke(app, ["note", "before select"])

    async def drive() -> tuple[int, int, int, bool]:
        app_cls = watch_mod._build_app_class(interval=60.0)
        async with app_cls().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            feed = pilot.app.query_one("#feed", OptionList)
            before = feed.option_count
            await pilot.press("v")  # freeze into selectable text view
            await pilot.pause()
            runner.invoke(app, ["note", "arrives while frozen"])
            pilot.app.board.refresh_data()
            await pilot.pause()
            frozen = feed.option_count  # unchanged: select mode is stable
            select_visible = pilot.app.query_one("#feedtext").has_class("active")
            await pilot.press("v")  # back to live: backlog applies
            await pilot.pause()
            return before, frozen, feed.option_count, select_visible

    before, frozen, after, select_visible = asyncio.run(drive())
    assert select_visible
    assert frozen == before  # nothing appended mid-selection
    assert after == before + 1  # the frozen-period event landed on resume


def test_autoscroll_toggle(runner: CliRunner, work_dir: Path) -> None:
    pytest.importorskip("textual", reason="the [tui] extra is not installed")
    from aisquare.cli import watch as watch_mod

    team_service.activate()

    async def drive() -> tuple[bool, bool]:
        app_cls = watch_mod._build_app_class(interval=60.0)
        async with app_cls().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            initial = pilot.app.board.autoscroll
            await pilot.press("a")
            await pilot.pause()
            return initial, pilot.app.board.autoscroll

    initial, toggled = asyncio.run(drive())
    assert initial is True and toggled is False


def test_done_archive_view_shows_who_and_when(
    runner: CliRunner, work_dir: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("textual", reason="the [tui] extra is not installed")
    from textual.widgets import DataTable

    from aisquare.cli import watch as watch_mod
    from aisquare.cli.ui import board as board_mod

    monkeypatch.setenv("AISQUARE_ROLE", "runner")
    runner.invoke(
        app,
        ["hook", "session-start"],
        input=json.dumps(
            {
                "cwd": str(work_dir),
                "session_id": "ffff9999-0000-0000-0000-000000000000",
                "source": "startup",
                "transcript_path": str(work_dir / "transcript.jsonl"),
            }
        ),
    )
    monkeypatch.delenv("AISQUARE_ROLE")
    runner.invoke(app, ["task", "add", "finished thing"])
    task_id = json.loads(runner.invoke(app, ["--json", "task", "list"]).stdout)[0]["id"]
    runner.invoke(app, ["task", "done", task_id, "--as", "ffff9999"])

    async def drive() -> tuple[list[Any], int, str]:
        app_cls = watch_mod._build_app_class(interval=60.0)
        async with app_cls().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            table = pilot.app.query_one("#tasks", DataTable)
            # No open task: the one row is the empty state, keyed as no task is (#286).
            open_rows = [key.value for key in table.rows]
            await pilot.press("d")  # flip to the done archive
            await pilot.pause()
            table.focus()
            await pilot.press("enter")  # select the (row 0) archived task
            await pilot.pause()
            return open_rows, table.row_count, pilot.app.board.detail_text

    open_rows, done_rows, detail = asyncio.run(drive())
    assert open_rows == [board_mod.EMPTY_ROW] and done_rows == 1
    assert "finished thing" in detail
    assert "done by ffff9999" in detail  # who + when from the task_done event


def test_transcript_line_near_finds_the_moment(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from aisquare.cli.watch import transcript_line_near

    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps({"type": "x", "timestamp": f"2026-07-06T10:0{i}:00Z"}) for i in range(6)
        ),
        encoding="utf-8",
    )
    at = datetime(2026, 7, 6, 10, 3, 30, tzinfo=UTC)
    assert transcript_line_near(transcript, at) == 5  # first line at/after 10:03:30
    late = datetime(2026, 7, 6, 23, 0, 0, tzinfo=UTC)
    assert transcript_line_near(transcript, late) == 6  # past the end → last line
    assert transcript_line_near(tmp_path / "missing.jsonl", at) == 1


def test_feed_selection_survives_a_refresh_tick(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("textual", reason="the [tui] extra is not installed")
    from textual.widgets import DataTable, OptionList

    from aisquare.cli import watch as watch_mod

    team_service.activate()
    # Two task rows so the cursor sits on row >= 1 — the case a per-key guard
    # misses because clear() re-posts RowHighlighted for row 0 (round-4).
    runner.invoke(app, ["task", "add", "task one"])
    runner.invoke(app, ["task", "add", "task two"])
    runner.invoke(app, ["note", "a feed line to select", "--kind", "decision"])

    async def drive() -> tuple[object, object]:
        app_cls = watch_mod._build_app_class(interval=60.0)
        async with app_cls().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            table = pilot.app.query_one("#tasks", DataTable)
            table.focus()
            table.move_cursor(row=1)  # NOT row 0
            await pilot.press("enter")  # select the task
            await pilot.pause()
            feed = pilot.app.query_one("#feed", OptionList)
            feed.focus()
            feed.highlighted = feed.option_count - 1
            await pilot.press("enter")  # select the feed line
            await pilot.pause()
            chosen = pilot.app.board.detail_moment
            pilot.app.board.refresh_data()  # the async RowHighlighted clobber path
            await pilot.pause()
            return chosen, pilot.app.board.detail_moment

    chosen, after_tick = asyncio.run(drive())
    assert chosen is not None
    assert after_tick == chosen  # feed selection preserved across the rebuild


def test_watch_does_not_leak_a_project_pin_to_environ(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("textual", reason="the [tui] extra is not installed")
    import os

    from aisquare.cli import watch as watch_mod

    monkeypatch.delenv("AISQUARE_TEAM_HUB", raising=False)
    team_service.activate()

    async def run() -> None:
        app_cls = watch_mod._build_app_class(interval=60.0)
        async with app_cls().run_test(size=(120, 40)) as pilot:
            await pilot.pause()

    asyncio.run(run())
    assert "AISQUARE_TEAM_HUB" not in os.environ  # no process-wide leak


def test_terminal_cache_flushes_when_a_task_closes(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("textual", reason="the [tui] extra is not installed")
    from aisquare.cli import watch as watch_mod

    team_service.activate()

    async def drive() -> bool:
        app_cls = watch_mod._build_app_class(interval=60.0)
        async with app_cls().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            pilot.app.board._terminal_by_task = {"warm": object()}  # a stale cache
            runner.invoke(app, ["task", "add", "closes soon"])
            tid = json.loads(runner.invoke(app, ["--json", "task", "list"]).stdout)[0]["id"]
            runner.invoke(app, ["task", "done", tid])
            pilot.app.board.refresh_data()  # the task_done event arrives on the feed
            await pilot.pause()
            return bool(pilot.app.board._terminal_by_task == {})

    assert asyncio.run(drive())  # a close flushes the attribution cache


def test_attribution_negative_cache_avoids_refetch_every_tick(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("textual", reason="the [tui] extra is not installed")
    from aisquare.cli import watch as watch_mod
    from aisquare.services import team as team_service

    team_service.activate()
    calls = {"n": 0}
    real = team_service.terminal_attribution

    def counting(**kwargs: object) -> object:
        calls["n"] += 1
        return real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(team_service, "terminal_attribution", counting)

    async def drive() -> int:
        app_cls = watch_mod._build_app_class(interval=60.0)
        async with app_cls().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            for _ in range(5):  # a task with no terminal event, looked up repeatedly
                pilot.app.board._done_event_for("tsk_never_closed")
            return calls["n"]

    assert asyncio.run(drive()) == 1  # fetched once, negative result cached


def test_single_click_shows_task_detail(runner: CliRunner, work_dir: Path) -> None:
    """Round-7 spec: every detail population must come from pilot mouse/keys.

    (1) cursor parked on row 0, a real click on NON-cursored row 2 shows THAT
    row's detail; (2) header and blank-area clicks never touch a selected
    feed detail; (3) refresh ticks never touch it either.
    """
    pytest.importorskip("textual", reason="the [tui] extra is not installed")
    from datetime import datetime

    from textual.widgets import OptionList

    from aisquare.cli import watch as watch_mod

    team_service.activate()
    _add(runner, "alpha task")
    _add(runner, "beta task")
    _add(runner, "gamma task")
    runner.invoke(app, ["note", "the feed line to keep", "--kind", "decision"])
    tasks = json.loads(runner.invoke(app, ["--json", "task", "list"]).stdout)
    gamma = next(t for t in tasks if t["title"] == "gamma task")
    gamma_updated = datetime.fromisoformat(gamma["updated_at"])

    # Widget-relative rows: y=0 border, y=1 header, y=2 row0 ... => row2 at y=4.
    async def drive() -> tuple[str, object, str, object, str, object, str, object]:
        app_cls = watch_mod._build_app_class(interval=60.0)
        async with app_cls().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # (1) cursor is parked at row 0 by the initial rebuild; one real
            # click on row 2 must show row 2's detail.
            await pilot.click("#tasks", offset=(6, 4))
            await pilot.pause()
            clicked_text = pilot.app.board.detail_text
            clicked_moment = pilot.app.board.detail_moment
            # (2) select a feed line (keys), then click the table header and
            # the blank area below the rows: the feed detail must survive.
            feed = pilot.app.query_one("#feed", OptionList)
            feed.focus()
            feed.highlighted = feed.option_count - 1
            await pilot.press("enter")
            await pilot.pause()
            feed_text = pilot.app.board.detail_text
            feed_moment = pilot.app.board.detail_moment
            await pilot.click("#tasks", offset=(6, 1))  # header
            await pilot.click("#tasks", offset=(6, 9))  # blank below the rows
            await pilot.pause()
            after_stray_text = pilot.app.board.detail_text
            after_stray_moment = pilot.app.board.detail_moment
            # (3) several refresh ticks: still untouched.
            for _ in range(3):
                pilot.app.board.refresh_data()
                await pilot.pause()
            return (
                clicked_text,
                clicked_moment,
                feed_text,
                feed_moment,
                after_stray_text,
                after_stray_moment,
                pilot.app.board.detail_text,
                pilot.app.board.detail_moment,
            )

    (
        clicked_text,
        clicked_moment,
        feed_text,
        feed_moment,
        stray_text,
        stray_moment,
        final_text,
        final_moment,
    ) = asyncio.run(drive())
    # (1) the click on the non-cursored row showed THAT row, exactly.
    assert "gamma task" in clicked_text
    assert "alpha task" not in clicked_text
    assert clicked_moment == (None, gamma_updated)  # unclaimed todo: claimant None
    # (2) header + blank clicks left the selected feed detail untouched...
    assert "the feed line to keep" in feed_text
    assert stray_text == feed_text
    assert stray_moment == feed_moment
    # (3) ...and so did the refresh ticks.
    assert final_text == feed_text
    assert final_moment == feed_moment


def test_the_theme_reader_survives_a_state_file_that_is_not_an_object(isolated_home: Path) -> None:
    """`_load_saved_theme` raised `AttributeError` on such a file (`.get` on a list), one
    line into the fleet UI's mount."""
    from aisquare.cli import watch as watch_mod

    isolated_home.mkdir(parents=True, exist_ok=True)
    path = isolated_home / "state.json"
    body = '["was", "a", "list"]\n'
    path.write_text(body)
    assert watch_mod._load_saved_theme() is None
    assert path.read_text() == body


def test_the_board_says_once_when_the_theme_cannot_be_remembered(
    runner: CliRunner, work_dir: Path, isolated_home: Path
) -> None:
    """The save's refusal was dropped: the picker showed the theme applied, the file refused
    it, and the board said nothing. The save is debounced and runs on a worker, so the test
    waits for both."""
    pytest.importorskip("textual", reason="the [tui] extra is not installed")
    from aisquare.cli import watch as watch_mod

    team_service.activate()
    isolated_home.mkdir(parents=True, exist_ok=True)
    body = '["was", "a", "list"]\n'
    (isolated_home / "state.json").write_text(body)

    async def pick() -> tuple[int, str]:
        app_cls = watch_mod._build_app_class(interval=60.0)
        async with app_cls().run_test(size=(120, 40), notifications=True) as pilot:
            await pilot.pause()
            # What was said, recorded as it is raised: the refusal is told to the loop from the
            # saver's thread, and a toast read off the screen after one pause was not there yet.
            told = toast_log.record(pilot.app)
            for name in ("nord", "dracula"):
                pilot.app.theme = name
                assert await asyncio.to_thread(pilot.app._theme_autosave.settled, 5.0)
                await settle_page(pilot.app)
            return len(told), told[0] if told else ""

    count, text = asyncio.run(pick())
    assert count == 1 and "the theme could not be saved; it will be retried" in text
    assert (isolated_home / "state.json").read_text() == body


def test_the_board_flushes_a_theme_picked_inside_the_debounce_at_quit(
    runner: CliRunner, work_dir: Path, isolated_home: Path
) -> None:
    """The save is debounced; a `t`, a pick and a `q` within a tenth of a second must still land."""
    pytest.importorskip("textual", reason="the [tui] extra is not installed")
    from aisquare.cli import watch as watch_mod

    team_service.activate()

    async def pick_and_quit() -> None:
        app_cls = watch_mod._build_app_class(interval=60.0)
        async with app_cls().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            pilot.app.theme = "nord"  # and out, before the debounce fires

    asyncio.run(pick_and_quit())
    assert json.loads((isolated_home / "state.json").read_text())["board_theme"] == "nord"


def test_the_board_launcher_says_what_the_quit_could_not_save(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from aisquare.cli import watch as watch_mod

    class _Quit:
        def __init__(self) -> None:
            self.unsaved = ["the theme was not saved: state.json is not a JSON object"]

        def run(self) -> None:
            return None

    monkeypatch.setattr(watch_mod, "_build_app_class", lambda interval, project=None: _Quit)
    watch_mod._run_tui(60.0)
    assert "⚠ the theme was not saved: state.json is not a JSON object" in capsys.readouterr().err


_SAVE_HANG_GUARD_S = 30.0


def _on_file(app: Any) -> None:
    """Every preference the gestures asked for is on file when the board is left.

    The same flush the quit makes (``Autosave.flush_all``), with a hang guard for its
    deadline instead of the quit's 3 s. A test that reads ``state.json`` once the board has
    quit was reading what the quit's 3 s join happened to wait for: the last drag's write is
    in flight at that moment (the debounce fires during the final settle), and a write that
    stalls past 3 s, as an fsync on a loaded windows-latest runner does, is given up on:
    ``KeyError: 'board_team_height'`` with the value in memory (CI run 40355260127). Stalled
    for 4 s, it fails the same way here. The quit's own deadline is the subject of
    ``test_the_board_flushes_a_theme_picked_inside_the_debounce_at_quit``, which keeps it."""
    from aisquare.cli.ui.autosave import Autosave

    unsaved = Autosave.flush_all(app, timeout=_SAVE_HANG_GUARD_S)
    assert unsaved == [], unsaved


async def _mouse(pilot: Any, kind: Any, x: int, y: int) -> None:
    """One mouse event the way the driver delivers it — through ``App.on_event``.

    ``Pilot``'s mouse helpers hand their events straight to the screen, past the
    click-chain counter that turns two releases into a double click, and their
    moves carry no button; a drag needs both (``tests/test_ui_shell.py`` says the same).
    """
    from tests.ui_workers import settle_page

    pilot.app.post_message(kind(None, x, y, 0, 0, 1, False, False, False))
    await pilot.app.wait_for_refresh()
    await settle_page(pilot.app)


async def _drag(pilot: Any, start: tuple[int, int], end: tuple[int, int]) -> None:
    from textual import events

    await _mouse(pilot, events.MouseDown, *start)
    await _mouse(pilot, events.MouseMove, *end)
    await _mouse(pilot, events.MouseUp, *end)


def _board_sizes(app: Any) -> tuple[int, int, int]:
    """(board column's width, detail pane's height, team block's height) as laid out."""
    return (
        app.query_one("#board-side").outer_size.width,
        app.query_one("#detailwrap").outer_size.height,
        app.query_one("#sessions").outer_size.height,
    )


def test_every_board_line_drags_and_each_size_comes_back_at_the_next_launch(
    runner: CliRunner, work_dir: Path, isolated_home: Path
) -> None:
    """The human: every bar between the board's parts drags to resize — the detail pane up,
    the board column against the live feed left and right, the team block over the tasks —
    and the board shows those sizes again at the next launch."""
    pytest.importorskip("textual", reason="the [tui] extra is not installed")
    from aisquare.cli import watch as watch_mod
    from aisquare.cli.ui.board import COLUMN_WIDTH_KEY, DETAIL_HEIGHT_KEY, TEAM_HEIGHT_KEY

    team_service.activate()
    app_cls = watch_mod._build_app_class(interval=60.0)

    async def drag_all() -> tuple[tuple[int, int, int], tuple[int, int, int]]:
        async with app_cls().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app = pilot.app
            before = _board_sizes(app)
            side = app.query_one("#side-divider").region
            await _drag(pilot, (side.x, 10), (70, 10))  # the column's right edge to column 70
            detail = app.query_one("#detail-divider").region
            await _drag(pilot, (10, detail.y), (10, 15))  # the detail pane's top up to row 15
            team = app.query_one("#team-divider").region
            await _drag(pilot, (10, team.y), (10, team.y + 5))  # the team block 5 rows taller
            _on_file(app)
            return before, _board_sizes(app)

    async def relaunch() -> tuple[int, int, int]:
        async with app_cls().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            for divider in ("#side-divider", "#detail-divider", "#team-divider"):
                handle = pilot.app.query_one(divider)
                await handle.wait_for_refresh()  # the restore queued at mount has run
                await handle.wait_for_refresh()  # and the layout it asked for is done
            return _board_sizes(pilot.app)

    before, dragged = asyncio.run(drag_all())
    assert before == (48, 8, 3), "the premise: the stylesheet's sizes"
    # 39 rows above the footer: a line at row 15 leaves the detail pane rows 16-38.
    assert dragged == (70, 23, 8)
    state = json.loads((isolated_home / "state.json").read_text())
    assert (state[COLUMN_WIDTH_KEY], state[DETAIL_HEIGHT_KEY], state[TEAM_HEIGHT_KEY]) == (
        70,
        23,
        8,
    )
    assert asyncio.run(relaunch()) == (70, 23, 8)


def test_a_double_click_on_a_board_line_puts_the_stylesheet_size_back_and_forgets_it(
    runner: CliRunner, work_dir: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("textual", reason="the [tui] extra is not installed")
    from textual import events
    from textual.app import App as TextualApp

    from aisquare.cli import watch as watch_mod
    from aisquare.cli.ui.board import COLUMN_WIDTH_KEY, DETAIL_HEIGHT_KEY, TEAM_HEIGHT_KEY

    team_service.activate()
    # Two clicks read as a double click within this window; its length is not what this
    # tests, so it is wide enough for a loaded runner.
    monkeypatch.setattr(TextualApp, "CLICK_CHAIN_TIME_THRESHOLD", 5.0)
    app_cls = watch_mod._build_app_class(interval=60.0)

    async def drag_then_reset() -> tuple[tuple[int, int, int], tuple[int, int, int]]:
        async with app_cls().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app = pilot.app
            side = app.query_one("#side-divider").region
            await _drag(pilot, (side.x, 10), (70, 10))
            detail = app.query_one("#detail-divider").region
            await _drag(pilot, (10, detail.y), (10, 15))
            team = app.query_one("#team-divider").region
            await _drag(pilot, (10, team.y), (10, team.y + 5))
            dragged = _board_sizes(app)
            for x, y in ((70, 10), (10, 15), (10, team.y + 5)):
                for _ in range(2):
                    await _mouse(pilot, events.MouseDown, x, y)
                    await _mouse(pilot, events.MouseUp, x, y)
            _on_file(app)
            return dragged, _board_sizes(app)

    dragged, reset = asyncio.run(drag_then_reset())
    assert dragged == (70, 23, 8), "the premise: every line moved"
    assert reset == (48, 8, 3), "two clicks on each line: the stylesheet's sizes"
    state = json.loads((isolated_home / "state.json").read_text())
    assert not {COLUMN_WIDTH_KEY, DETAIL_HEIGHT_KEY, TEAM_HEIGHT_KEY} & state.keys(), (
        "a reset forgets the size, so the next launch shows the stylesheet's"
    )


def test_b_hides_and_brings_back_the_board_column_every_time(
    runner: CliRunner, work_dir: Path
) -> None:
    """``b`` with the tasks table focused hid the column the table is in, focus went to
    nothing, and no later key reached the panel whose binding ``b`` is: the column stayed
    hidden until a restart (found verifying the splitters). Focus now goes to the feed as
    the column hides and back to the tasks as it returns — in select mode, to the frozen
    feed that stands in for the live one."""
    pytest.importorskip("textual", reason="the [tui] extra is not installed")
    from aisquare.cli import watch as watch_mod

    team_service.activate()
    app_cls = watch_mod._build_app_class(interval=60.0)

    async def press_b() -> list[tuple[bool, bool, str | None]]:
        async with app_cls().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app = pilot.app
            app.query_one("#tasks").focus()
            await pilot.pause()

            def seen() -> tuple[bool, bool, str | None]:
                focused = app.focused
                return (
                    app.query_one("#board-side").display,
                    app.query_one("#side-divider").display,
                    focused.id if focused is not None else None,
                )

            states = [seen()]
            for _ in range(4):
                await pilot.press("b")
                await pilot.pause()
                states.append(seen())
            await pilot.press("v")  # select mode: the live feed gives way to its text
            await pilot.pause()
            await pilot.press("b")
            await pilot.pause()
            states.append(seen())
            return states

    states = asyncio.run(press_b())
    assert states == [
        (True, True, "tasks"),
        (False, False, "feed"),
        (True, True, "tasks"),
        (False, False, "feed"),
        (True, True, "tasks"),
        (False, False, "feedtext"),
    ]


def test_the_board_lines_keep_the_feed_and_the_tasks_on_screen_whatever_the_size(
    runner: CliRunner, work_dir: Path
) -> None:
    """A line dragged as far as it goes stops where the other side keeps its minimum — the
    feed 32 columns, the board above the detail pane 8 rows, the tasks 4 — and a terminal
    that shrinks re-clamps it; too narrow for the column, the column and its line hide."""
    pytest.importorskip("textual", reason="the [tui] extra is not installed")
    from aisquare.cli import watch as watch_mod

    team_service.activate()
    app_cls = watch_mod._build_app_class(interval=60.0)

    async def push() -> dict[str, object]:
        async with app_cls().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app = pilot.app

            def laid_out() -> tuple[int, int, int, int, int]:
                return (
                    app.query_one("#board-side").outer_size.width,
                    app.query_one("#feedpane").outer_size.width,
                    app.query_one("#detailwrap").outer_size.height,
                    app.query_one("#board-main").outer_size.height,
                    app.query_one("#tasks").outer_size.height,
                )

            seen: dict[str, object] = {}
            team = app.query_one("#team-divider").region
            # The team block to 20 rows: below the "needs you" line over the board (#286).
            strip = app.query_one("#needs-you").outer_size.height
            await _drag(pilot, (10, team.y), (10, 20 + strip))
            seen["team"] = app.query_one("#sessions").outer_size.height
            side = app.query_one("#side-divider").region
            await _drag(pilot, (side.x, 10), (119, 10))  # the column all the way right
            detail = app.query_one("#detail-divider").region
            await _drag(pilot, (10, detail.y), (10, 0))  # the detail pane all the way up
            seen["pushed"] = laid_out()
            seen["team_squeezed"] = app.query_one("#sessions").outer_size.height
            await pilot.resize_terminal(100, 30)
            await pilot.pause()
            await app.wait_for_refresh()
            seen["shrunk"] = laid_out()
            await pilot.resize_terminal(70, 30)
            await pilot.pause()
            seen["narrow"] = (
                app.query_one("#board-side").display,
                app.query_one("#side-divider").display,
            )
            await pilot.resize_terminal(120, 40)
            # The panel's own Resize shows the column again, and on windows-latest it came
            # after the pause: `b` found the column still hidden and showed it (CI run
            # 39041194777). The press waits for the column it is about to hide.
            side = app.query_one("#board-side")
            for _ in range(100):
                await pilot.pause(0.05)
                if not side.has_class("collapsed"):
                    break
            seen["widened"] = not side.has_class("collapsed")
            app.board.action_toggle_board()  # `b`
            await pilot.pause()
            seen["toggled"] = (
                app.query_one("#board-side").display,
                app.query_one("#side-divider").display,
            )
            return seen

    seen = asyncio.run(push())
    assert seen["team"] == 20, "the premise: the team block grew past its undragged ten rows"
    # 120 columns: 87 + the line + 32 for the feed. 39 rows: 8 above, the line, 30 below.
    assert seen["pushed"] == (87, 32, 29, 8, 4), "the detail pane less the needs-you line"
    assert seen["team_squeezed"] == 3, "8 rows of board: the line and 4 rows of tasks left 3"
    # 100 x 30: 67 + 1 + 32 columns; 29 rows are 8 + 1 + 20.
    assert seen["shrunk"] == (67, 32, 19, 8, 4)
    assert seen["narrow"] == (False, False), "the column and the line that resizes it hide"
    assert seen["widened"], "the premise: 120 columns show the column again"
    assert seen["toggled"] == (False, False)


def _statuses_on_the_board(runner: CliRunner) -> dict[str, str]:
    """One task in every state, by title; returns title → id."""
    ids = {}
    for title in ("plan it", "build it", "check it", "stuck on it", "after it", "ship it"):
        ids[title] = _add(runner, title)
    ids["wait for it"] = _add(runner, "wait for it", "--needs", ids["after it"])
    runner.invoke(app, ["task", "claim", ids["build it"]])
    runner.invoke(app, ["task", "claim", ids["check it"]])
    runner.invoke(app, ["task", "review", ids["check it"]])
    runner.invoke(app, ["task", "block", ids["stuck on it"], "--reason", "no key"])
    runner.invoke(app, ["task", "done", ids["ship it"]])
    dropped = _add(runner, "forget it")
    runner.invoke(app, ["task", "drop", dropped])
    ids["forget it"] = dropped
    return ids


def test_the_tasks_table_is_the_number_an_icon_and_the_title(
    runner: CliRunner, work_dir: Path
) -> None:
    """The human: "status can be just icons, no text … so just id, status and title". The
    number on the board, one icon per state (a todo waiting on a need gets its own), the
    title; no who. The done archive keeps its time beside the same icons."""
    pytest.importorskip("textual", reason="the [tui] extra is not installed")
    from aisquare.cli import watch as watch_mod
    from aisquare.cli.ui.board import STATUS_ICONS

    team_service.activate()
    ids = _statuses_on_the_board(runner)
    numbers = {
        title: json.loads(runner.invoke(app, ["--json", "task", "show", task]).stdout)["number"]
        for title, task in ids.items()
    }

    def rows(table: Any) -> dict[str, tuple[str, str]]:
        return {
            str(row[2].plain): (str(row[0].plain), str(row[1].plain).strip())
            for row in (table.get_row_at(i) for i in range(table.row_count))
        }

    async def drive() -> tuple[list[str], dict[str, tuple[str, str]], dict[str, tuple[str, str]]]:
        app_cls = watch_mod._build_app_class(interval=60.0)
        async with app_cls().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            table = pilot.app.query_one("#tasks")
            labels = [column.label.plain for column in table.columns.values()]
            open_rows = rows(table)
            pilot.app.board.action_toggle_done()
            await pilot.pause()
            return labels, open_rows, rows(table)

    labels, open_rows, archive = asyncio.run(drive())
    icon = {state: glyph for state, (glyph, _style, _meaning) in STATUS_ICONS.items()}
    assert labels == ["#", "st", "title"]
    assert open_rows == {
        "plan it": (str(numbers["plan it"]), icon["todo"]),
        "build it": (str(numbers["build it"]), icon["doing"]),
        "check it": (str(numbers["check it"]), icon["review"]),
        "stuck on it": (str(numbers["stuck on it"]), icon["blocked"]),
        "after it": (str(numbers["after it"]), icon["todo"]),
        "wait for it": (str(numbers["wait for it"]), icon["waiting"]),
    }
    assert set(archive) == {"ship it", "forget it"}
    assert archive["ship it"][0] == str(numbers["ship it"])
    assert archive["ship it"][1].startswith(icon["done"]) and ":" in archive["ship it"][1]
    assert archive["forget it"][1].startswith(icon["dropped"])


def test_a_long_title_is_cut_to_the_table_and_the_detail_says_the_rest(
    runner: CliRunner, work_dir: Path
) -> None:
    """The title takes the rest of the width and ends in an ellipsis where it would not
    fit — the table never scrolls sideways, even after the column is narrowed — and the
    detail pane's header gives what the row left out: number, status, id, claimant."""
    pytest.importorskip("textual", reason="the [tui] extra is not installed")
    from aisquare.cli import watch as watch_mod

    team_service.activate()
    long_title = "wire the accounts page to the five-minute usage cache " * 3
    task_id = _add(runner, long_title.strip())
    runner.invoke(app, ["task", "claim", task_id])
    number = json.loads(runner.invoke(app, ["--json", "task", "show", task_id]).stdout)["number"]

    async def drive() -> tuple[str, int, str, int, str]:
        app_cls = watch_mod._build_app_class(interval=60.0)
        async with app_cls().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            table = pilot.app.query_one("#tasks")
            wide = (str(table.get_row_at(0)[2].plain), int(table.max_scroll_x))
            side = pilot.app.query_one("#side-divider").region
            await _drag(pilot, (side.x, 10), (36, 10))  # narrow the column
            await pilot.pause()
            narrow = (str(table.get_row_at(0)[2].plain), int(table.max_scroll_x))
            await pilot.click("#tasks", offset=(6, 2))
            await pilot.pause()
            detail = str(pilot.app.board.detail_text).splitlines()[1]
            return (*wide, *narrow, detail)

    wide_title, wide_scroll, narrow_title, narrow_scroll, detail = asyncio.run(drive())
    assert wide_title.endswith("…") and narrow_title.endswith("…")
    assert len(narrow_title) < len(wide_title), "the title refits when the column narrows"
    assert wide_scroll == 0 and narrow_scroll == 0, "never a sideways scroll"
    assert detail.startswith(f"#{number} · doing · {task_id} · claimed by ")


def test_question_mark_opens_the_boards_keys_and_its_icon_legend(
    runner: CliRunner, work_dir: Path
) -> None:
    pytest.importorskip("textual", reason="the [tui] extra is not installed")
    from textual.widgets import Static

    from aisquare.cli import watch as watch_mod
    from aisquare.cli.ui.board import STATUS_ICONS, BoardHelp

    team_service.activate()

    async def drive() -> tuple[bool, str, bool]:
        app_cls = watch_mod._build_app_class(interval=60.0)
        async with app_cls().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            opened = isinstance(pilot.app.screen, BoardHelp)
            text = str(pilot.app.screen.query_one(Static).render())
            await pilot.press("escape")
            await pilot.pause()
            return opened, text, isinstance(pilot.app.screen, BoardHelp)

    opened, text, still_open = asyncio.run(drive())
    assert opened and not still_open
    for glyph, _style, meaning in STATUS_ICONS.values():
        assert f"{glyph}" in text and meaning in text
    assert "done archive" in text and "themes" in text


@NEEDS_POSIX
def test_c_copies_the_selected_feed_onto_the_native_clipboard_and_osc52(
    runner: CliRunner, work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hotfix 0.8.2: the board's copy reaches pbcopy beside its OSC 52 — ``aisquare watch``
    is a ``ClipboardApp`` too, not only the fleet's shell — and says so once it has."""
    pytest.importorskip("textual", reason="the [tui] extra is not installed")
    from textual.selection import SELECT_ALL
    from textual.widgets import Static

    from aisquare.cli import watch as watch_mod

    team_service.activate()
    runner.invoke(app, ["note", TEXT])
    board = install(tmp_path, monkeypatch)
    app_cls = watch_mod._build_app_class(interval=60.0)

    async def drive() -> tuple[str, list[str], list[str]]:
        async with app_cls().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            writes: list[str] = []
            monkeypatch.setattr(pilot.app._driver, "write", writes.append)
            await pilot.press("v")  # select mode: the feed as text to select
            await pilot.pause()
            pilot.app.screen.selections = {pilot.app.query_one("#feedstatic", Static): SELECT_ALL}
            await pilot.pause()
            selected = pilot.app.screen.get_selected_text()
            await pilot.press("c")
            for _ in range(400):  # the toast is raised once the native write is over
                if pilot.app._notifications:
                    break
                await pilot.pause(0.05)
            told = [note.message for note in pilot.app._notifications]
            return selected or "", told, [w for w in writes if "\x1b]52;" in w]

    selected, told, osc52 = asyncio.run(drive())
    assert TEXT in selected, "the premise: the note is in what was selected"
    assert board.texts() == [selected]
    encoded = base64.b64encode(selected.encode("utf-8")).decode("ascii")
    assert osc52 == [f"\x1b]52;c;{encoded}\a"]
    assert told == [f"copied {len(selected)} chars"]
