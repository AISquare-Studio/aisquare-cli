"""The live board: dependency readiness, feed rendering, and the TUI smoke."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.cli.watch import feed_line
from aisquare.services import team as team_service


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
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
            table.move_cursor(row=1)
            await pilot.pause()
            return table.row_count, feed.option_count, pilot.app.detail_text

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
            return still_open, applied, str(pilot.app.theme)

    still_open, applied, final = asyncio.run(browse())
    assert still_open  # selection does NOT close the dialog
    assert applied == final  # the browsed theme stuck after closing
    saved = json.loads((isolated_home / "state.json").read_text())["board_theme"]
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
    assert "<svg" in shots[0].read_text()[:200]


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
            pilot.app._refresh_data()
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
            initial = pilot.app._autoscroll
            await pilot.press("a")
            await pilot.pause()
            return initial, pilot.app._autoscroll

    initial, toggled = asyncio.run(drive())
    assert initial is True and toggled is False
