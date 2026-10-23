"""The ⬆ row under *⚙ Settings*, its dialog, and the one-click update's hand-over and reopen.

The release check is the app's ``release_check`` seam (``services.updates.check_for_release`` in
production, tested in tests/test_release_check.py), and the plan the dialog is built on is
``lifecycle.upgrade_plan``'s, replaced here. The press's record is the real store's. What
leaves the process — PyPI, uv, the exec into ``aisquare upgrade`` — is closed by
``no_real_installer``, and the hand-over is read off ``app.hand_off``, which ``run_ui``
execs once the app has quit.
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading
from collections.abc import Awaitable, Callable, Collection
from pathlib import Path
from typing import Any, TypeVar

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, Static
from typer.testing import CliRunner

from aisquare.cli import fleet as fleet_cli
from aisquare.cli import install as install_cli
from aisquare.cli.app import app as cli_app
from aisquare.cli.ui import app as ui_app
from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.sidebar import (
    ALIVE_STATES,
    UPDATE_KEY,
    SettingsRow,
    StatusLine,
    UpdateRow,
)
from aisquare.cli.ui.update import PRESS_WORKER, UpdateScreen
from aisquare.cli.ui.views.doctor import DoctorView
from aisquare.models import FleetAgent
from aisquare.services import install_route, lifecycle, updates
from aisquare.services.install_route import Facts, InstallRoute, LatestRelease
from aisquare.services.lifecycle import UpgradePlan
from tests.installer_seams import no_real_installer  # noqa: F401 — autouse, applied by import
from tests.rendered import plain
from tests.test_lifecycle_upgrade import Machine, Tool, machine, tool  # noqa: F401
from tests.test_ui_doctor_handoff import Terminal, terminal  # noqa: F401
from tests.test_ui_shell import Script, no_real_tmux, script, seed, shown, status  # noqa: F401
from tests.ui_workers import settle_page, settle_until

T = TypeVar("T")

OFFER = updates.Offer(version="0.9.1", current="0.9.0", runnable=True, announce=True)
_FACTS = Facts(
    prefix=Path("/tools/aisquare-cli"),
    base_prefix=Path("/usr"),
    executable=Path("/tools/aisquare-cli/bin/python"),
    platform="darwin",
    python_version="3.12",
)


def _plan(*, runnable: bool = True, remote: str | None = None) -> UpgradePlan:
    if runnable:
        route = InstallRoute(install_route.UV_TOOL, _FACTS)
        argv: tuple[str, ...] = ("uv", "tool", "install", "--force", "aisquare-cli>=0.9.0")
        reason = None
    else:
        route = InstallRoute(install_route.HOMEBREW, _FACTS, formula="aisquare-cli")
        argv = ("brew", "upgrade", "aisquare-cli")
        reason = "Homebrew installs are upgraded with brew"
    return UpgradePlan(
        route=route,
        current="0.9.0",
        target=None,
        latest=LatestRelease("0.9.1"),
        argv=argv,
        env={},
        reason=reason,
        remote=remote,
    )


@pytest.fixture
def planned(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """``upgrade_plan`` as the dialog asks it: answered with :func:`_plan`, each ask kept."""
    asked: list[dict[str, Any]] = []

    def plan(target: str | None = None, **kwargs: Any) -> UpgradePlan:
        asked.append({"target": target, **kwargs})
        return _plan()

    monkeypatch.setattr(lifecycle, "upgrade_plan", plan)
    return asked


def _drive(
    fn: Callable[[Pilot[None], FleetApp], Awaitable[T]],
    *,
    offer: updates.Offer | None = OFFER,
    release_check: Callable[[], updates.Offer | None] | None = None,
    after_update: bool = False,
    size: tuple[int, int] = (140, 40),
) -> T:
    async def run() -> T:
        app = FleetApp(
            refresh_seconds=3600,
            doctor=lambda: [],
            release_check=release_check or (lambda: offer),
            after_update=after_update,
        )
        async with app.run_test(size=size, notifications=True) as pilot:
            await settle_page(app)
            return await fn(pilot, app)

    return asyncio.run(run())


async def _toasts(app: FleetApp) -> list[str]:
    from textual.widgets._toast import Toast

    await settle_page(app)
    return [toast.render().plain for toast in app.screen.query(Toast)]


async def _open_by_keyboard(pilot: Pilot[None], app: FleetApp) -> UpdateScreen:
    """↓ from the top of the sidebar until the cursor is on the ⬆ row, then Enter."""
    app.sidebar.focus()
    for _ in range(30):
        await pilot.press("down")
        if app.sidebar._cursor_key == UPDATE_KEY:
            break
    assert app.sidebar._cursor_key == UPDATE_KEY, "the ⬆ row is reachable from the keyboard"
    await pilot.press("enter")
    await settle_page(app)
    screen = app.screen
    assert isinstance(screen, UpdateScreen), f"the dialog opened, not {screen!r}"
    return screen


# --- the row and its toast --------------------------------------------------------------------


@pytest.mark.parametrize("size", [(90, 30), (140, 40)])
def test_the_row_sits_under_settings_above_the_status_line(size: tuple[int, int]) -> None:
    async def body(pilot: Pilot[None], app: FleetApp) -> tuple[str, int, int, int]:
        row = app.query_one(UpdateRow)
        settings = app.query_one(SettingsRow)
        line = app.query_one(StatusLine)
        return shown(row), settings.region.y, row.region.y, line.region.y

    text, settings_y, row_y, line_y = _drive(body, size=size)

    assert text == "⬆︎ 0.9.1 · restart to update"
    assert (row_y - settings_y, line_y - row_y) == (1, 1)


def test_no_offer_no_row_and_no_toast() -> None:
    async def body(pilot: Pilot[None], app: FleetApp) -> tuple[bool, list[str]]:
        return app.query_one(UpdateRow).display, await _toasts(app)

    displayed, toasts = _drive(body, offer=None)

    assert displayed is False
    assert not any("is out" in toast for toast in toasts)


def test_the_first_find_of_a_release_toasts_once() -> None:
    async def body(pilot: Pilot[None], app: FleetApp) -> list[str]:
        return await _toasts(app)

    first = _drive(body)
    later = _drive(body, offer=updates.Offer("0.9.1", "0.9.0", runnable=True, announce=False))

    said = "aisquare 0.9.1 is out (you run 0.9.0) — ⬆ in the sidebar restarts asq to update"
    assert said in first
    assert not any("is out" in toast for toast in later), "announced already: the row alone"


def test_an_install_asq_does_not_update_is_told_how_not_to_restart() -> None:
    offer = updates.Offer("0.9.1", "0.9.0", runnable=False, announce=True)

    async def body(pilot: Pilot[None], app: FleetApp) -> tuple[str, list[str]]:
        return shown(app.query_one(UpdateRow)), await _toasts(app)

    text, toasts = _drive(body, offer=offer)

    assert text == "⬆︎ 0.9.1 · how to update"
    assert any("⬆ in the sidebar says how to update this install" in toast for toast in toasts)


def test_the_hourly_ask_takes_the_row_away_once_nothing_newer_is_offered() -> None:
    """The second answer, as the hourly tick asks it, after an update made elsewhere."""
    answers: list[updates.Offer | None] = [OFFER, None]

    async def body(pilot: Pilot[None], app: FleetApp) -> tuple[bool, bool]:
        before = app.query_one(UpdateRow).display
        app.check_release()
        await settle_page(app)
        return before, app.query_one(UpdateRow).display

    assert _drive(body, release_check=lambda: answers.pop(0)) == (True, False)
    assert answers == []


# --- the dialog -------------------------------------------------------------------------------


def _two_alive(tmp_path: Path, scripted: Script) -> None:
    seed(tmp_path, ("prj_a", "a", "able-ant"))
    scripted["prj_a"] = [
        status("prj_a", "coder-1", "coder", "working"),
        status("prj_a", "reviewer-1", "reviewer", "waiting"),
        status("prj_a", "old-1", "coder", "exited", exit_status=0),
    ]


@pytest.mark.parametrize("key", ["escape", "cancel"])
def test_the_row_opens_the_dialog_and_cancel_or_escape_changes_nothing(
    tmp_path: Path,
    script: Script,  # noqa: F811
    planned: list[dict[str, Any]],
    key: str,
) -> None:
    _two_alive(tmp_path, script)

    async def body(pilot: Pilot[None], app: FleetApp) -> tuple[str, str, list[str], bool]:
        screen = await _open_by_keyboard(pilot, app)
        title = str(screen.query_one("#update-box").border_title)
        said = shown(screen.query_one("#update-text", Static))
        buttons = [str(b.label) for b in screen.query(Button)]
        if key == "escape":
            await pilot.press("escape")
        else:
            await pilot.click("#update-cancel")
        await settle_page(app)
        return title, said, buttons, isinstance(app.screen, UpdateScreen)

    title, said, buttons, still_open = _drive(body)

    assert title == "Update aisquare 0.9.0 → 0.9.1"
    assert said == (
        "asq closes, installs 0.9.1 and reopens here.\n"
        "2 agents keep running; any whose pane is gone afterwards is restarted on its session."
    )
    assert buttons == ["Restart & update", "Cancel"]
    assert planned == [{"target": None, "known_latest": LatestRelease("0.9.1")}], (
        "the plan is built on the check's answer: PyPI is not asked again"
    )
    assert still_open is False
    assert updates.take_press() is None, "nothing recorded"


def test_a_remote_on_elsewhere_adds_its_warning_short(
    tmp_path: Path,
    script: Script,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _two_alive(tmp_path, script)
    monkeypatch.setattr(
        lifecycle, "upgrade_plan", lambda *a, **k: _plan(remote="aisquare remote serve on port 8")
    )

    async def body(pilot: Pilot[None], app: FleetApp) -> str:
        screen = await _open_by_keyboard(pilot, app)
        return shown(screen.query_one("#update-text", Static))

    said = _drive(body)

    assert said.endswith(
        "⚠ Remote is on (aisquare remote serve on port 8): it can fail while the install is "
        "replaced, and runs 0.9.0 until it is started again."
    )


def test_an_install_asq_does_not_update_shows_the_command_and_no_restart(
    tmp_path: Path,
    script: Script,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _two_alive(tmp_path, script)
    monkeypatch.setattr(lifecycle, "upgrade_plan", lambda *a, **k: _plan(runnable=False))

    async def body(pilot: Pilot[None], app: FleetApp) -> tuple[str, list[str], Any]:
        screen = await _open_by_keyboard(pilot, app)
        said = shown(screen.query_one("#update-text", Static))
        buttons = [str(b.label) for b in screen.query(Button)]
        await pilot.click("#update-cancel")
        await settle_page(app)
        return said, buttons, app.hand_off

    said, buttons, hand_off = _drive(body)

    assert "brew upgrade aisquare-cli" in said
    assert said.startswith("asq does not update this install itself (Homebrew installs are")
    assert buttons == ["Close"]
    assert hand_off is None and updates.take_press() is None


@pytest.fixture
def alive_now(monkeypatch: pytest.MonkeyPatch, script: Script) -> list[Collection[str]]:  # noqa: F811
    """``updates.agents_alive_now`` as the press reads it: the script's rows that are alive
    NOW, which a test changes after the frame was painted. Each ask's project ids are kept."""
    asked: list[Collection[str]] = []

    def alive(project_ids: Collection[str]) -> list[FleetAgent]:
        asked.append(set(project_ids))
        return [
            row.agent
            for project_id in project_ids
            for row in script.get(project_id, [])
            if row.state in ALIVE_STATES
        ]

    monkeypatch.setattr(updates, "agents_alive_now", alive)
    return asked


def test_restart_and_update_records_the_agents_alive_at_the_press_then_hands_over(
    tmp_path: Path,
    script: Script,  # noqa: F811
    planned: list[dict[str, Any]],
    alive_now: list[Collection[str]],
) -> None:
    """The record is read at the press, not taken from the frame: reviewer-1 was alive in the
    frame the dialog counted from and exited before the press, so the reopen must not restart
    it (review of #301, m1). The hand-over is Doctor's, with ``--one-click``."""
    _two_alive(tmp_path, script)

    async def body(pilot: Pilot[None], app: FleetApp) -> tuple[Any, str]:
        screen = await _open_by_keyboard(pilot, app)
        counted = shown(screen.query_one("#update-text", Static))
        script["prj_a"][1] = status("prj_a", "reviewer-1", "reviewer", "exited", exit_status=0)
        await pilot.click("#update-confirm")
        await settle_page(app)
        return app.hand_off, counted

    hand_off, counted = _drive(body)

    assert "2 agents keep running" in counted, "the dialog counted from the frame"
    assert hand_off == ("upgrade", "--one-click")
    assert alive_now == [{"prj_a"}], "the frame's projects, read again at the press"
    press = updates.take_press()
    assert press is not None
    assert [(k.agent_id, k.pane_id, k.label) for k in press.agents] == [
        ("agt_a_coder-1", "%1", "coder-1"),
    ], "only the agent still alive at the press; the one that exited since is left alone"


def test_a_refused_hand_over_leaves_no_record_behind(
    tmp_path: Path,
    script: Script,  # noqa: F811
    planned: list[dict[str, Any]],
    alive_now: list[Collection[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A writer still running refuses the hand-over, as for Doctor's Update; the record the
    press wrote goes with it (review of #301, m3)."""
    _two_alive(tmp_path, script)
    monkeypatch.setattr(FleetApp, "_work_running", lambda self, names: "a fix")

    async def body(pilot: Pilot[None], app: FleetApp) -> tuple[Any, list[str]]:
        await _open_by_keyboard(pilot, app)
        await pilot.click("#update-confirm")
        return app.hand_off, await _toasts(app)

    hand_off, toasts = _drive(body)

    assert hand_off is None
    assert "a fix is still running — try again when it ends" in toasts
    assert alive_now == [{"prj_a"}], "control: the press did read and record"
    assert updates.take_press() is None


def test_only_the_dialogs_own_press_hands_over() -> None:
    """A worker of the press's name that this dialog did not start (a stray, a test's
    stand-in) hands nothing over, whatever it answers: an update runs on a confirmed press."""

    async def body(pilot: Pilot[None], app: FleetApp) -> Any:
        app.run_worker(
            lambda: updates.Press(previous="0.9.0", agents=()),
            name=PRESS_WORKER,
            thread=True,
            exit_on_error=False,
        )
        await settle_page(app)
        return app.hand_off

    assert _drive(body) is None


def test_an_update_called_off_takes_its_record_with_it() -> None:
    """A second press past the wait stays in asq: the record of the update that will not run
    goes; Doctor's own hand-over called off leaves the store alone (control)."""

    async def body(pilot: Pilot[None], app: FleetApp) -> tuple[bool, bool]:
        updates.record_press([])
        app.hand_off = ("upgrade", "--reopen")
        app._call_off_hand_off()
        doctors = updates.take_press() is not None
        updates.record_press([])
        app.hand_off = ui_app.UPDATE_HAND_OFF
        app._call_off_hand_off()
        return doctors, updates.take_press() is None

    doctors_kept, update_dropped = _drive(body, offer=None)

    assert doctors_kept and update_dropped


def test_an_update_kept_waiting_names_the_row_to_press_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The overdue toast says what to press: on the ⬆ path that is the row, not Doctor's
    buttons; pressed, the row calls the hand-over off as Doctor's second press does."""
    now = [1000.0]
    monkeypatch.setattr(ui_app, "_hand_off_clock", lambda: now[0])
    release = threading.Event()

    async def body(pilot: Pilot[None], app: FleetApp) -> tuple[list[str], Any]:
        said: list[str] = []
        app.notify = lambda message, **_: said.append(str(message))  # type: ignore[method-assign]
        app.exit = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        app.run_worker(release.wait, name="hung-read", thread=True, exit_on_error=False)
        app.on_doctor_view_hand_off(DoctorView.HandOff(ui_app.UPDATE_HAND_OFF))
        now[0] += ui_app._HAND_OFF_PATIENCE
        await settle_until(app, lambda: len(said) == 2, group="held")
        app.query_one(UpdateRow).activate()
        await settle_page(app, group="held")
        release.set()
        await settle_page(app)
        return said, app.hand_off

    said, hand_off = _drive(body)

    assert said == [
        "asq quits once the reads running end",
        "Still waiting for a read after 10 s. Press the ⬆ row again to stay in asq; quit, and "
        "it runs once they end.",
        "Called off: asq carries on",
    ], said
    assert hand_off is None


def test_the_row_says_what_the_dialogs_plan_says(
    tmp_path: Path,
    script: Script,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check found a runnable install; by the press uv had gone from PATH. The dialog
    and the row both follow the plan: Close only, and the row says how to update."""
    _two_alive(tmp_path, script)
    gone = dataclasses.replace(_plan(), reason="uv is not on PATH")
    monkeypatch.setattr(lifecycle, "upgrade_plan", lambda *a, **k: gone)

    async def body(pilot: Pilot[None], app: FleetApp) -> tuple[list[str], str, bool]:
        screen = await _open_by_keyboard(pilot, app)
        buttons = [str(b.label) for b in screen.query(Button)]
        row = shown(app.query_one(UpdateRow))
        assert app.release is not None
        return buttons, row, app.release.runnable

    buttons, row, runnable = _drive(body)

    assert buttons == ["Close"]
    assert row == "⬆︎ 0.9.1 · how to update"
    assert runnable is False


def test_a_ctrl_c_in_the_quit_names_the_command_without_asqs_own_flag() -> None:
    said = ui_app._hand_off_undone(ui_app.UPDATE_HAND_OFF)

    assert said == "⚠ Update did not run: asq quit at the Ctrl-C — `aisquare upgrade` runs it"


# --- the reopen -------------------------------------------------------------------------------


def test_the_reopen_reattaches_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    outcome = updates.Reattached(
        previous="0.9.0", version="0.9.1", kept=("coder-1",), restarted=(("coder-2", "pane gone"),)
    )
    asked: list[str] = []

    def reattach() -> updates.Reattached:
        asked.append("reattach")
        return outcome

    monkeypatch.setattr(updates, "reattach_after_update", reattach)

    async def body(pilot: Pilot[None], app: FleetApp) -> list[str]:
        return await _toasts(app)

    toasts = _drive(body, offer=None, after_update=True)
    ordinary = _drive(body, offer=None)

    assert asked == ["reattach"], "only the reopen looks: an ordinary start restarts nothing"
    assert "✓ aisquare 0.9.1 — 1 agent reattached, 1 restarted (coder-2: pane gone)" in toasts
    assert not any("reattached" in toast for toast in ordinary)


# --- the hidden flags -------------------------------------------------------------------------

_ENTER = "Press Enter to go back to asq "


def test_one_click_upgrades_without_asking_and_reopens_at_once_to_reattach(
    runner: CliRunner,
    tool: Tool,  # noqa: F811
    machine: Machine,  # noqa: F811
    terminal: Terminal,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_question(*_a: object, **_k: object) -> bool:
        raise AssertionError("--one-click asks nothing")

    monkeypatch.setattr("aisquare.cli.install.typer.confirm", no_question)

    result = runner.invoke(cli_app, ["upgrade", "--one-click"])

    assert result.exit_code == 0, result.output
    assert len(machine.installs) == 1
    assert terminal.prompts == [], "no Press Enter on success"
    assert terminal.reopened == [["ui", "--after-update"]]


def test_one_click_with_nothing_newer_reopens_at_once(
    runner: CliRunner,
    tool: Tool,  # noqa: F811
    machine: Machine,  # noqa: F811
    terminal: Terminal,  # noqa: F811
) -> None:
    machine.latest = LatestRelease("0.9.0")

    result = runner.invoke(cli_app, ["upgrade", "--one-click"])

    assert result.exit_code == 0 and "nothing to do" in result.stdout, result.output
    assert machine.installs == [] and terminal.prompts == []
    assert terminal.reopened == [["ui", "--after-update"]]


def test_a_failed_one_click_waits_for_enter_then_reopens_the_old_version(
    runner: CliRunner,
    tool: Tool,  # noqa: F811
    machine: Machine,  # noqa: F811
    terminal: Terminal,  # noqa: F811
) -> None:
    machine.installer_exit = 1

    result = runner.invoke(cli_app, ["upgrade", "--one-click"])

    assert result.exit_code == 1 and "the upgrade failed" in result.stderr, result.output
    assert terminal.prompts == [_ENTER], "the failure is read before asq comes back"
    assert terminal.reopened == [["ui", "--after-update"]]


def test_a_ctrl_c_at_a_failed_one_clicks_prompt_stays_in_the_shell(
    runner: CliRunner,
    tool: Tool,  # noqa: F811
    machine: Machine,  # noqa: F811
    terminal: Terminal,  # noqa: F811
) -> None:
    machine.installer_exit = 1
    terminal.answer = KeyboardInterrupt()

    result = runner.invoke(cli_app, ["upgrade", "--one-click"])

    assert result.exit_code == 1
    assert terminal.prompts == [_ENTER] and terminal.reopened == []


@pytest.mark.parametrize(
    ("raised", "said"),
    [
        (
            KeyboardInterrupt(),
            "✗ the upgrade was interrupted. Run it again by hand: aisquare upgrade",
        ),
        (
            RuntimeError("uv vanished"),
            "✗ the upgrade stopped on an error: RuntimeError: uv vanished. Run it again by "
            "hand: aisquare upgrade",
        ),
    ],
)
def test_a_one_click_cut_short_says_so_waits_for_enter_and_reopens(
    runner: CliRunner,
    tool: Tool,  # noqa: F811
    machine: Machine,  # noqa: F811
    terminal: Terminal,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    raised: BaseException,
    said: str,
) -> None:
    """A Ctrl-C in uv's run, or an error nobody expected, is a failure like any: said, read
    before Enter, and asq back on the version still installed (review of #301, m2)."""

    def installer(*_args: object, **_kwargs: object) -> int:
        raise raised

    monkeypatch.setattr(install_route, "run_installer", installer)

    result = runner.invoke(cli_app, ["upgrade", "--one-click"])

    assert result.exit_code != 0
    assert said in result.stderr, result.output
    assert terminal.prompts == [_ENTER], "the failure is read before asq comes back"
    assert terminal.reopened == [["ui", "--after-update"]]


def test_one_click_says_the_fleet_keeps_running_without_advising_a_shutdown(
    runner: CliRunner,
    tool: Tool,  # noqa: F811
    machine: Machine,  # noqa: F811
    terminal: Terminal,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keeping the agents running is what the ⬆ update is for: one factual line, not the
    advice to stop them first that ``--yes`` prints (control)."""
    monkeypatch.setattr(
        lifecycle, "running_fleet", lambda: (("coder-1 (proj)", "reviewer-1 (proj)"), 0, None)
    )

    one_click = runner.invoke(cli_app, ["upgrade", "--one-click"])
    plain_yes = runner.invoke(cli_app, ["upgrade", "--yes"])

    assert one_click.exit_code == 0 and plain_yes.exit_code == 0, one_click.output
    line = "· 2 agents keep running; a hook they fire during the install is lost"
    assert line in one_click.stdout
    assert "fleet shutdown" not in one_click.stdout
    assert "fleet shutdown" in plain_yes.stdout and line not in plain_yes.stdout


def test_upgrade_without_the_flag_is_as_before(
    runner: CliRunner,
    tool: Tool,  # noqa: F811
    machine: Machine,  # noqa: F811
    terminal: Terminal,  # noqa: F811
) -> None:
    """Control: Doctor's Update, ``--yes --reopen``, still waits for Enter and opens plain ui."""
    result = runner.invoke(cli_app, ["upgrade", "--yes", "--reopen"])

    assert result.exit_code == 0, result.output
    assert terminal.prompts == [_ENTER] and terminal.reopened == [["ui"]]


@pytest.mark.parametrize(
    ("argv", "flag", "control"),
    [
        (["upgrade", "--help"], "--one-click", "--dry-run"),
        (["ui", "--help"], "--after-update", "Open"),
    ],
)
def test_the_new_flags_are_hidden(
    runner: CliRunner, argv: list[str], flag: str, control: str
) -> None:
    shown_help = runner.invoke(cli_app, argv)
    text = plain(shown_help.stdout)

    assert shown_help.exit_code == 0 and control in text, "control: the help is there"
    assert flag not in text


def test_ui_after_update_tells_the_app(monkeypatch: pytest.MonkeyPatch, runner: CliRunner) -> None:
    opened: list[dict[str, Any]] = []
    monkeypatch.setattr(fleet_cli, "interactive_terminal", lambda: True)
    monkeypatch.setattr(ui_app, "run_ui", lambda **options: opened.append(options))

    after = runner.invoke(cli_app, ["ui", "--after-update"])
    ordinary = runner.invoke(cli_app, ["ui"])

    assert after.exit_code == 0 and ordinary.exit_code == 0, after.output + ordinary.output
    assert opened == [{"after_update": True}, {}], "an ordinary start is run as before"


def test_the_one_click_reopen_names_a_ui_that_takes_it() -> None:
    """``upgrade --one-click`` execs ``ui --after-update``: the flag it names is the ui's."""
    assert install_cli.ONE_CLICK_REOPEN == ("ui", "--after-update")
