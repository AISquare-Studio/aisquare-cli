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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, TypeVar

import pytest
from textual.pilot import Pilot
from textual.timer import Timer
from textual.widgets import Button, Static, Switch
from textual.worker import Worker
from typer.testing import CliRunner

from aisquare.cli import fleet as fleet_cli
from aisquare.cli import install as install_cli
from aisquare.cli.app import app as cli_app
from aisquare.cli.ui import app as ui_app
from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.remote_control import RemoteController
from aisquare.cli.ui.sidebar import (
    ALIVE_STATES,
    UPDATE_KEY,
    AppSettingsSelected,
    SettingsRow,
    StatusLine,
    UpdateRow,
)
from aisquare.cli.ui.update import (
    CHECK_WORKER,
    CHECKING,
    PLAN_WORKER,
    PRESS_WORKER,
    REFRESH_DEADLINE_SECONDS,
    REFRESH_WORKER,
    UNCONFIRMED,
    UpdateScreen,
)
from aisquare.cli.ui.views.doctor import DoctorView
from aisquare.core import selfcli
from aisquare.core.config import load_config
from aisquare.models import FleetAgent
from aisquare.services import install_route, lifecycle, updates
from aisquare.services.install_route import Facts, InstallRoute, LatestRelease
from aisquare.services.lifecycle import UpgradePlan
from tests.installer_seams import no_real_installer  # noqa: F401 — autouse, applied by import
from tests.rendered import plain
from tests.test_lifecycle_upgrade import Machine, Tool, _hooked, _record, machine, tool  # noqa: F401
from tests.test_release_check import _state
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


class Fresh:
    """``updates.refresh_latest`` as the dialog asks it: the answer to give, and a gate that
    holds the ask open until a test lets it land."""

    def __init__(self) -> None:
        self.answer: str | None = "0.9.1"
        self.gate = threading.Event()
        self.gate.set()
        self.asked = 0

    def __call__(self) -> str | None:
        self.asked += 1
        self.gate.wait(30)
        return self.answer


@pytest.fixture(autouse=True)
def fresh(monkeypatch: pytest.MonkeyPatch) -> Fresh:
    """The dialog's ask of PyPI as it opens: PyPI says the release the row offers unless a
    test says otherwise. Its deadline is a minute, so a test that holds the ask on a loaded
    runner is not overtaken by it; the deadline's own test sets it short."""
    stand_in = Fresh()
    monkeypatch.setattr(updates, "refresh_latest", stand_in)
    monkeypatch.setattr(ui_app, "REFRESH_DEADLINE_SECONDS", 60.0)
    return stand_in


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


async def _toasts(app: FleetApp, *, group: str | None = None) -> list[str]:
    """The toasts on screen once the page settles: ``group``'s workers alone, when given."""
    from textual.widgets._toast import Toast

    await settle_page(app, group=group)
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


@pytest.mark.parametrize("held", [None, "off", "on"], ids=["idle", "across-off", "across-on"])
def test_the_release_check_switch_takes_the_row_away_and_brings_it_back_at_once(
    monkeypatch: pytest.MonkeyPatch, held: str | None
) -> None:
    """Review of #301, m2: saved off in App settings, the ⬆ row stayed up and clickable, and a
    click asked PyPI, until the hourly ask; saved on again, it waited for that ask too. The
    save takes the row away at once, and asks again at once. A check asked before a save
    and answered after it answers for the setting that was: across the off save it brings
    no row back, and across the on save its "none" is dropped and the check asked again."""
    asks: list[bool] = []
    hold = threading.Event()
    hold.set()

    def check() -> updates.Offer | None:
        enabled = load_config().updates.check
        asks.append(enabled)
        hold.wait(30)
        return OFFER if enabled else None

    async def save(app: FleetApp, on: bool) -> None:
        if held == ("on" if on else "off"):
            hold.clear()  # asked before this save, answered after it
            app.check_release()
        app.query_one("#updates-check", Switch).value = on
        app.query_one("#save-settings", Button).press()
        # Messages only: the check this test holds open is a worker settle_page waits for.
        await settle_page(app, group="no-worker-of-this-name")
        hold.set()

    async def body(pilot: Pilot[None], app: FleetApp) -> list[object]:
        seen: list[object] = [app.query_one(UpdateRow).display]
        app.post_message(AppSettingsSelected())
        await settle_page(app)
        await save(app, False)
        await settle_page(app)
        seen += [app.query_one(UpdateRow).display, app.release]
        await save(app, True)
        await settle_until(app, lambda: app.query_one(UpdateRow).display, timeout=5)
        seen += [app.query_one(UpdateRow).display, asks[-1]]
        return seen

    seen = _drive(body, release_check=check)

    assert seen == [True, False, None, True, True], (seen, asks)
    assert load_config().updates.check is True


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
        "the plan is built on the check's answer, offline: PyPI is asked once, by the refresh"
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

    def alive(project_ids: Collection[str], seen: Collection[FleetAgent] = ()) -> list[FleetAgent]:
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
    assert hand_off == ("upgrade", "--one-click"), "unpinned, as `aisquare upgrade` installs"
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


def test_only_the_dialogs_own_press_hands_over(
    tmp_path: Path,
    script: Script,  # noqa: F811
    planned: list[dict[str, Any]],
) -> None:
    """The dialog was opened and cancelled; then a worker of the press's name that no press
    started (a stray, a test's stand-in) answers with a record: it hands nothing over, and
    leaves no record. An update runs on a confirmed press alone."""
    _two_alive(tmp_path, script)

    async def body(pilot: Pilot[None], app: FleetApp) -> Any:
        await _open_by_keyboard(pilot, app)
        await pilot.click("#update-cancel")
        await settle_page(app)
        app.run_worker(
            lambda: updates.Press(previous="0.9.0", agents=()),
            name=PRESS_WORKER,
            thread=True,
            exit_on_error=False,
        )
        await settle_page(app)
        return app.hand_off

    assert _drive(body) is None
    assert len(planned) == 1, "control: the dialog did open on its plan"


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


def test_no_dialog_opens_over_a_hand_off_set_while_its_plan_was_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #301, n3: the row was clicked, and Doctor's Update handed over while the
    dialog's plan was being read. The click checks for a hand-over, the plan's answer did
    not, and the dialog opened over it. It opens none; the dialog's ask is let go."""
    gate = threading.Event()
    asked: list[bool] = []

    def plan(*_args: Any, **_kwargs: Any) -> UpgradePlan:
        asked.append(True)
        gate.wait(30)
        return _plan()

    monkeypatch.setattr(lifecycle, "upgrade_plan", plan)

    async def body(pilot: Pilot[None], app: FleetApp) -> tuple[bool, object]:
        app.query_one(UpdateRow).activate()
        await settle_until(app, lambda: asked, group="no-worker-of-this-name")
        app.hand_off = ("upgrade", "--reopen")  # Doctor's Update, as it was pressed
        gate.set()
        await settle_page(app)
        return isinstance(app.screen, UpdateScreen), app._refresh_worker

    opened, refresh = _drive(body)

    assert opened is False, "no dialog over the hand-over"
    assert refresh is None, "the dialog's ask is let go"


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


_LAST_CHECKED = (
    "asq closes, installs the newest release (0.9.1 when last checked) and reopens here."
)


@pytest.mark.parametrize(
    ("answer", "title", "says"),
    [
        ("0.9.2", "Update aisquare 0.9.0 → 0.9.2", "asq closes, installs 0.9.2 and reopens here."),
        ("0.9.1", "Update aisquare 0.9.0 → 0.9.1", "asq closes, installs 0.9.1 and reopens here."),
        (None, "Update aisquare 0.9.0", f"{_LAST_CHECKED}\n"),
    ],
    ids=["newer-since", "as-checked", "no-answer"],
)
def test_the_dialog_asks_pypi_as_it_opens_and_restart_waits_for_the_answer(
    tmp_path: Path,
    script: Script,  # noqa: F811
    planned: list[dict[str, Any]],
    fresh: Fresh,
    answer: str | None,
    title: str,
    says: str,
) -> None:
    """The dialog opens on the last check's number (a day old, maybe) and asks PyPI. Until the
    answer lands, *Restart & update* is off and no number is named as the one installed: the
    update installs uv's newest, unpinned, and a probe pressed while the dialog named 0.9.1
    installed 0.9.2 (review of #301, m1). PyPI's answer is then named, and moves the row; no
    answer turns the button on, worded as the newest release, the last check's number as that.
    """
    _two_alive(tmp_path, script)
    fresh.answer = answer
    fresh.gate.clear()

    async def body(pilot: Pilot[None], app: FleetApp) -> tuple[Any, ...]:
        app.sidebar.focus()
        app.query_one(UpdateRow).activate()
        # The ask is held: settle the plan's read alone, until the dialog it opens is up.
        await settle_until(app, lambda: isinstance(app.screen, UpdateScreen), group=PLAN_WORKER)
        screen = app.screen
        assert isinstance(screen, UpdateScreen)
        waiting = shown(screen.query_one("#update-text", Static))
        before = str(screen.query_one("#update-box").border_title)
        held = screen.query_one("#update-confirm", Button).disabled
        await pilot.click("#update-confirm")
        await settle_page(app, group=PRESS_WORKER)
        pressed = (app.hand_off, isinstance(app.screen, UpdateScreen))
        fresh.gate.set()
        await settle_page(app)
        after = str(screen.query_one("#update-box").border_title)
        said = shown(screen.query_one("#update-text", Static))
        enabled = not screen.query_one("#update-confirm", Button).disabled
        return waiting, before, held, pressed, after, said, enabled, shown(app.query_one(UpdateRow))

    waiting, before, held, pressed, after, said, enabled, row = _drive(body)

    assert waiting.startswith(_LAST_CHECKED), waiting
    assert waiting.endswith("checking PyPI for the newest release…")
    assert before == "Update aisquare 0.9.0", "no number named before PyPI answers"
    assert held is True and pressed == (None, True), "Restart waits for PyPI's answer"
    assert fresh.asked == 1
    assert after == title
    assert says in said and "checking" not in said
    if answer is None:
        assert said.endswith("not confirmed: PyPI did not answer just now")
    assert enabled is True
    if answer == "0.9.2":
        assert row == "⬆︎ 0.9.2 · restart to update", "the row follows PyPI's answer"


@pytest.mark.parametrize("lands", ["dialog-open", "before-the-plan"])
def test_pypi_saying_nothing_is_newer_closes_the_dialog_and_says_so(
    tmp_path: Path,
    script: Script,  # noqa: F811
    fresh: Fresh,
    monkeypatch: pytest.MonkeyPatch,
    lands: str,
) -> None:
    """PyPI answers that 0.9.0, the version that runs, is the newest (the check's 0.9.1 was
    yanked, say). Whether the answer lands with the dialog up or before the plan's read, the
    row goes, no dialog stays, and a toast says why: landing first, the click opened nothing
    and the row went in silence (review of #301, m2)."""
    _two_alive(tmp_path, script)
    fresh.answer = "0.9.0"
    gate = fresh.gate if lands == "dialog-open" else threading.Event()
    gate.clear()
    asked: list[int] = []

    def plan(*_a: Any, **_k: Any) -> UpgradePlan:
        asked.append(1)
        if lands == "before-the-plan":
            gate.wait(30)
        return _plan()

    monkeypatch.setattr(lifecycle, "upgrade_plan", plan)

    async def body(pilot: Pilot[None], app: FleetApp) -> tuple[bool, list[str], bool, Any]:
        app.sidebar.focus()
        app.query_one(UpdateRow).activate()
        if lands == "dialog-open":
            await settle_until(app, lambda: isinstance(app.screen, UpdateScreen), group=PLAN_WORKER)
        else:
            await settle_until(app, lambda: app._refreshed is not None, group=REFRESH_WORKER)
        opened = isinstance(app.screen, UpdateScreen)
        gate.set()
        await settle_page(app)
        stacked = any(isinstance(screen, UpdateScreen) for screen in app.screen_stack)
        return opened, await _toasts(app), stacked, (app.release, app.query_one(UpdateRow).display)

    opened, toasts, stacked, (release, row) = _drive(body)

    assert asked == [1], "control: the plan was read"
    assert opened is (lands == "dialog-open"), "control: the answer landed where the test says"
    assert "aisquare 0.9.0 is the newest release — nothing to update" in toasts, toasts
    assert stacked is False, "no dialog, open or opening"
    assert release is None and row is False, "the row goes"
    assert updates.take_press() is None


class Asks:
    """``updates.refresh_latest`` asked by one dialog after another: each ask held by its own
    gate, and given its own answer."""

    def __init__(self, *answers: str | None) -> None:
        self.answers = list(answers)
        self.gates = [threading.Event() for _ in answers]
        self.asked = 0
        self.landed = 0

    def __call__(self) -> str | None:
        ask = self.asked
        self.asked += 1
        self.gates[ask].wait(30)
        self.landed += 1
        return self.answers[ask]


def _looks(screen: UpdateScreen) -> tuple[str, bool, str]:
    """The dialog's title, whether *Restart & update* is off, and what it says."""
    return (
        str(screen.query_one("#update-box").border_title),
        screen.query_one("#update-confirm", Button).disabled,
        shown(screen.query_one("#update-text", Static)),
    )


async def _open(app: FleetApp) -> UpdateScreen:
    """Activate the ⬆ row and wait for the dialog, its ask held."""
    app.sidebar.focus()
    app.query_one(UpdateRow).activate()
    await settle_until(app, lambda: isinstance(app.screen, UpdateScreen), group=PLAN_WORKER)
    screen = app.screen
    assert isinstance(screen, UpdateScreen), f"the dialog opened, not {screen!r}"
    return screen


async def _lands(app: FleetApp, asks: Asks, ask: int) -> None:
    """Let ``ask`` answer, and wait until its answer has been handled."""
    landed = asks.landed + 1
    asks.gates[ask].set()
    await settle_until(app, lambda: asks.landed == landed, group=PLAN_WORKER)
    await settle_page(app, group=PLAN_WORKER)


@pytest.mark.parametrize("closed_lands", ["while-this-one-asks", "after-this-one-answered"])
def test_a_closed_dialogs_answer_never_reaches_the_next_dialog(
    tmp_path: Path,
    script: Script,  # noqa: F811
    planned: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    closed_lands: str,
) -> None:
    """Opened, closed with Esc and opened again: the first dialog's ask is still out, and its
    answer is the closed dialog's. Taken as the open one's, a 0.9.1 turned *Restart & update*
    on while this dialog's own ask, which would say 0.9.2, was out, and a first ask that failed
    and landed last turned a confirmed 0.9.2 into "not confirmed" (review of #301, m4)."""
    _two_alive(tmp_path, script)
    first = "0.9.1" if closed_lands == "while-this-one-asks" else None
    asks = Asks(first, "0.9.2")
    monkeypatch.setattr(updates, "refresh_latest", asks)
    order = [0, 1] if closed_lands == "while-this-one-asks" else [1, 0]

    async def body(pilot: Pilot[None], app: FleetApp) -> tuple[list[Any], int, str]:
        await _open(app)
        await pilot.press("escape")
        await settle_page(app, group=PLAN_WORKER)
        screen = await _open(app)
        seen = []
        for ask in order:
            await _lands(app, asks, ask)
            seen.append(_looks(screen))
        return seen, asks.asked, shown(app.query_one(UpdateRow))

    seen, asked, row = _drive(body)

    assert asked == 2, "control: each dialog asked"
    confirmed = ("Update aisquare 0.9.0 → 0.9.2", False)
    if closed_lands == "while-this-one-asks":
        title, off, said = seen[0]
        assert (title, off) == ("Update aisquare 0.9.0", True), seen[0]
        assert said.endswith(CHECKING), "the closed dialog's 0.9.1 named nothing"
    else:
        assert seen[0][:2] == confirmed, "control: this dialog's own answer was named"
    assert seen[-1][:2] == confirmed, seen[-1]
    assert "installs 0.9.2" in seen[-1][2] and UNCONFIRMED not in seen[-1][2]
    assert row == "⬆︎ 0.9.2 · restart to update"


def test_an_answer_for_a_dialog_closed_since_changes_nothing(
    tmp_path: Path,
    script: Script,  # noqa: F811
    planned: list[dict[str, Any]],
    fresh: Fresh,
) -> None:
    """The dialog closed with its ask out, and none opened since: PyPI's answer, that nothing
    is newer, lands with no dialog to answer. The row and the toasts stay as the dialog left
    them; the lookup recorded itself as a check, which the next hourly ask reads."""
    _two_alive(tmp_path, script)
    fresh.answer = "0.9.0"
    fresh.gate.clear()

    async def body(pilot: Pilot[None], app: FleetApp) -> tuple[list[str], bool, bool, list[str]]:
        await _open(app)
        await pilot.press("escape")
        before = await _toasts(app, group=PLAN_WORKER)  # the ask still held
        out = any(w.group == REFRESH_WORKER and not w.is_finished for w in app.workers)
        fresh.gate.set()
        await settle_page(app)
        return before, out, app.query_one(UpdateRow).display, await _toasts(app)

    before, out, row, after = _drive(body)

    assert fresh.asked == 1 and out is True, "control: the ask was out as the dialog closed"
    assert row is True, "the row stays"
    assert after == before and not any("nothing to update" in toast for toast in after), after


def test_an_ask_unanswered_after_five_seconds_counts_as_unanswered(
    tmp_path: Path,
    script: Script,  # noqa: F811
    planned: list[dict[str, Any]],
    fresh: Fresh,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """By the clock, not by the lookup's own timeout, which is per read with DNS outside it:
    a server trickling its answer held *Restart & update* off for 36 s (review of #301, m5).
    Past the deadline the button is on and the dialog says it is not confirmed; PyPI's answer,
    landing after, changes nothing. Here the deadline is cut to 0.3 s.

    The button off before the deadline is shown, under the minute's deadline, by
    ``test_the_dialog_asks_pypi_as_it_opens_and_restart_waits_for_the_answer``. Read here, after
    an open that took longer than 0.3 s on a loaded runner, it was already on (ambient
    (proxy-down), run 40114070143)."""
    assert REFRESH_DEADLINE_SECONDS == 5.0, "what docs/install.md says"
    monkeypatch.setattr(ui_app, "REFRESH_DEADLINE_SECONDS", 0.3)
    _two_alive(tmp_path, script)
    fresh.answer = "0.9.2"
    fresh.gate.clear()

    async def body(pilot: Pilot[None], app: FleetApp) -> tuple[Any, ...]:
        screen = await _open(app)
        await settle_until(
            app,
            lambda: not screen.query_one("#update-confirm", Button).disabled,
            group=PLAN_WORKER,
        )
        overdue = _looks(screen)
        out = any(w.group == REFRESH_WORKER and not w.is_finished for w in app.workers)
        fresh.gate.set()
        await settle_page(app)
        return out, overdue, _looks(screen), shown(app.query_one(UpdateRow))

    out, overdue, landed, row = _drive(body)

    assert out is True, "control: the ask was still out as the button came on"
    assert overdue[:2] == ("Update aisquare 0.9.0", False), "on once the deadline passed"
    assert overdue[2].startswith(_LAST_CHECKED) and overdue[2].endswith(UNCONFIRMED), overdue
    assert fresh.asked == 1, "control: the ask landed, the one there was"
    assert landed == overdue, "the answer after the deadline named nothing"
    assert row == "⬆︎ 0.9.1 · restart to update"


def test_the_deadline_is_no_textual_timer(
    tmp_path: Path,
    script: Script,  # noqa: F811
    planned: list[dict[str, Any]],
    fresh: Fresh,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deadline is the event loop's own timer. On Windows a pending Textual timer holds a
    thread of asyncio's default executor while it sleeps, and stopping it takes another
    (``textual._win_sleep``): the deadline's took the runner's last, the dialog closing on
    "nothing newer" waited on it, and the toast saying so never showed (windows-latest, run
    40104926701, reproduced with that sleep emulated). Every Textual timer set from the click
    until the dialog shows is counted, as ``test_the_release_check_adds_no_timer_of_its_own``
    counts them."""
    deadline = 47.0  # no timer of the app's own has this length
    monkeypatch.setattr(ui_app, "REFRESH_DEADLINE_SECONDS", deadline)
    _two_alive(tmp_path, script)
    fresh.gate.clear()
    real = Timer.__init__
    timers: list[tuple[str, float]] = []
    counting = [False]

    def recording(self: Timer, event_target: Any, interval: float, *a: Any, **k: Any) -> None:
        if counting[0]:
            timers.append((type(event_target).__name__, interval))
        real(self, event_target, interval, *a, **k)

    monkeypatch.setattr(Timer, "__init__", recording)

    async def body(pilot: Pilot[None], app: FleetApp) -> tuple[str, bool, str]:
        counting[0] = True
        screen = await _open(app)
        counting[0] = False
        looks = _looks(screen)
        fresh.gate.set()
        await settle_page(app)
        return looks

    looks = _drive(body)

    assert looks[:2] == ("Update aisquare 0.9.0", True), "control: the ask was out"
    assert any(target == "UpdateScreen" for target, _ in timers), "control: timers were seen"
    assert not [t for t in timers if t[0] == "FleetApp" or t[1] == deadline], timers


def test_a_row_pressed_while_the_dialog_or_a_press_is_open_opens_no_second_one(
    tmp_path: Path,
    script: Script,  # noqa: F811
    planned: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One dialog at a time: the row activated while the dialog shows, or once *Restart &
    update* is writing down the agents, starts no second plan or dialog (review of #301)."""
    _two_alive(tmp_path, script)
    hold = threading.Event()

    def held(project_ids: Collection[str], seen: Collection[FleetAgent] = ()) -> list[FleetAgent]:
        hold.wait(30)
        return list(seen)

    monkeypatch.setattr(updates, "agents_alive_now", held)

    async def body(pilot: Pilot[None], app: FleetApp) -> tuple[int, int, Any]:
        await _open_by_keyboard(pilot, app)
        app.query_one(UpdateRow).activate()  # the dialog is up
        await settle_page(app)
        while_open = len(planned)
        await pilot.click("#update-confirm")
        await pilot.pause()
        app.query_one(UpdateRow).activate()  # the press is writing down the agents
        await settle_page(app, group=PLAN_WORKER)  # the press is held: wait for a plan alone
        while_pressing = len(planned)
        stacked = sum(isinstance(screen, UpdateScreen) for screen in app.screen_stack)
        hold.set()
        await settle_page(app)
        return while_open, while_pressing, (stacked, app.hand_off)

    while_open, while_pressing, (stacked, hand_off) = _drive(body)

    assert (while_open, while_pressing) == (1, 1), "no second plan was read"
    assert stacked == 0, "no second dialog over the press"
    assert hand_off == ("upgrade", "--one-click"), "control: the one press hands over"


def test_an_app_a_test_builds_runs_no_release_check_and_run_ui_runs_the_real_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check runs in the asq a person opens (``run_ui``), never in a FleetApp built without
    one: every UI test's app started a check worker and an hourly ask (Windows CI, 7 failures
    on bff5c05e)."""
    # Every start, read as it begins: a quick worker has left ``app.workers`` by the time the
    # app has settled, so a list read afterwards could not see the check's (sabotage M36).
    started: list[str] = []
    real_start = Worker._start

    def start(worker: Worker[Any], *args: Any, **kwargs: Any) -> None:
        started.append(str(worker.name))
        real_start(worker, *args, **kwargs)

    monkeypatch.setattr(Worker, "_start", start)

    async def run() -> tuple[bool, object]:
        app = FleetApp(refresh_seconds=3600, doctor=lambda: [])
        async with app.run_test(size=(140, 40)):
            await settle_page(app)
            return app.query_one(UpdateRow).display, app._release_check

    displayed, check = asyncio.run(run())
    assert started, "control: the app's own starts were seen"
    built: list[dict[str, Any]] = []

    class Built:
        hand_off = None
        unsaved: ClassVar[list[str]] = []
        remote = RemoteController()

        def run(self) -> None:
            return None

    def build(**options: Any) -> Built:
        built.append(options)
        return Built()

    monkeypatch.setattr(ui_app, "FleetApp", build)
    ui_app.run_ui(after_update=True)

    assert check is None and displayed is False
    assert CHECK_WORKER not in started, started
    assert built == [{"release_check": updates.check_for_release, "after_update": True}]


def test_the_release_check_adds_no_timer_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hourly ask rides the housekeeping tick. On Windows every pending Textual timer holds
    a thread of asyncio's default executor while it sleeps (``textual._win_sleep``), and a
    runner has eight: the check's own hourly interval took one for the app's whole life, and
    seven UI tests whose thread workers queued behind the timers failed on Windows CI, the same
    seven twice (runs 39991862850, 40013337378), reproduced with that sleep emulated.

    Every Textual timer is counted, whoever sets it and however (``set_interval``,
    ``set_timer``, a ``Timer`` built directly): a guard on ``FleetApp.set_interval`` alone
    passed a check that re-armed itself with ``set_timer`` (tester's review of #301)."""
    real = Timer.__init__
    timers: dict[bool, list[tuple[str, float, str]]] = {True: [], False: []}
    current: list[bool] = []

    def recording(self: Timer, event_target: Any, interval: float, *a: Any, **k: Any) -> None:
        callback = k.get("callback")
        what = getattr(callback, "__qualname__", type(callback).__name__)
        timers[current[-1]].append((type(event_target).__name__, interval, what))
        real(self, event_target, interval, *a, **k)

    monkeypatch.setattr(Timer, "__init__", recording)

    async def run(with_check: bool) -> None:
        current.append(with_check)
        check = (lambda: None) if with_check else None
        app = FleetApp(refresh_seconds=3600, doctor=lambda: [], release_check=check)
        async with app.run_test(size=(140, 40)):
            await settle_page(app)

    asyncio.run(run(True))
    asyncio.run(run(False))

    assert ("FleetApp", ui_app.HOUSEKEEPING_SECONDS, "FleetApp._housekeeping") in timers[True], (
        "control: the app's own timers were seen"
    )
    assert sorted(timers[True]) == sorted(timers[False]), "the check set a timer of its own"


def _ticks(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The housekeeping tick at 50 ms, each of its runs counted."""
    monkeypatch.setattr(ui_app, "HOUSEKEEPING_SECONDS", 0.05)
    ran: list[int] = []
    real = FleetApp._housekeeping

    def counted(self: FleetApp) -> None:
        ran.append(1)
        real(self)

    monkeypatch.setattr(FleetApp, "_housekeeping", counted)
    return ran


async def _until(pilot: Pilot[None], done: Callable[[], bool]) -> None:
    """Pause in 50 ms steps until ``done()`` holds, for up to 5 s."""
    for _ in range(100):
        if done():
            return
        await pilot.pause(0.05)


def test_the_housekeeping_tick_asks_for_a_release_only_once_the_hour_has_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The release check's ask rides the tick, gated on ``ASK_EVERY_SECONDS`` since the last:
    the start's ask, none on the ticks within the hour, one once it has passed. With the gate
    gone, an ask every 30 s passed every test (review of #301, m3)."""
    ticks = _ticks(monkeypatch)
    asked: list[int] = []

    def check() -> updates.Offer | None:
        asked.append(1)
        return OFFER

    async def body(pilot: Pilot[None], app: FleetApp) -> tuple[int, int, int]:
        await _until(pilot, lambda: len(ticks) >= 8)
        await settle_page(app)
        within = len(asked)
        app._release_asked_at -= updates.ASK_EVERY_SECONDS  # the hour has passed
        await _until(pilot, lambda: len(asked) >= 2)
        await settle_page(app)
        passed = len(asked)
        seen = len(ticks)
        await _until(pilot, lambda: len(ticks) >= seen + 8)
        await settle_page(app)
        return within, passed, len(asked)

    within, passed, after = _drive(body, release_check=check)

    assert len(ticks) >= 16, "control: the tick ran"
    assert within == 1, "only the start's ask within the hour"
    assert passed == 2, "the hour's ask"
    assert after == 2, "then none again until the next hour"


@pytest.mark.parametrize("with_check", [True, False], ids=["with-check", "no-check"])
def test_the_housekeeping_tick_runs_remotes_auto_off_without_waiting(
    monkeypatch: pytest.MonkeyPatch, with_check: bool
) -> None:
    """Remote's auto-off rides the same tick, with or without a release check, and never waits
    for a Remote to stop: the fleet UI froze for seconds while uvicorn and ngrok stopped. With
    the auto-off gone from the tick, a Remote whose timer ran out stayed on, and every test
    passed (review of #301, m3)."""
    ticks = _ticks(monkeypatch)
    waits: list[bool] = []

    def enforce(self: RemoteController, *, wait: bool = True) -> bool:
        waits.append(wait)
        return False

    monkeypatch.setattr(RemoteController, "enforce_auto_off", enforce)

    async def run() -> tuple[int, list[bool]]:
        app = FleetApp(
            refresh_seconds=3600,
            doctor=lambda: [],
            release_check=(lambda: None) if with_check else None,
        )
        async with app.run_test(size=(140, 40)) as pilot:
            await settle_page(app)
            await _until(pilot, lambda: len(ticks) >= 4)
            return len(ticks), list(waits)  # before the exit's own call

    ran, asked = asyncio.run(run())

    assert ran >= 4, "control: the tick ran"
    assert len(asked) >= ran - 1 and asked, "the auto-off is checked on every tick"
    assert not any(asked), "never waiting for a Remote to stop"


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


@pytest.mark.parametrize(
    ("outcome", "warning"),
    [
        (
            updates.Reattached(
                previous="0.9.0", version="0.9.1", left=(("coder-1", "stopped on purpose"),)
            ),
            False,
        ),
        (
            updates.Reattached(
                previous="0.9.0", version="0.9.1", failed=(("coder-1", "its row is gone"),)
            ),
            True,
        ),
    ],
    ids=["left", "control-failed"],
)
def test_an_agent_left_as_it_was_is_named_in_an_information_toast(
    monkeypatch: pytest.MonkeyPatch, outcome: updates.Reattached, warning: bool
) -> None:
    """The owner's choice for PR #301: an agent the reopen leaves, as Restart all leaves it,
    is named, and is no trouble; a restart that could not be done is a warning."""
    monkeypatch.setattr(updates, "reattach_after_update", lambda: outcome)

    async def body(pilot: Pilot[None], app: FleetApp) -> list[tuple[str, bool]]:
        from textual.widgets._toast import Toast

        await _toasts(app)
        return [
            (toast.render().plain, toast.has_class("-warning")) for toast in app.screen.query(Toast)
        ]

    toasts = _drive(body, offer=None, after_update=True)

    assert (outcome.toast(), warning) in toasts, toasts


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
    updates.record_press([])

    result = runner.invoke(cli_app, ["upgrade", "--one-click"])

    assert result.exit_code == 0, result.output
    assert len(machine.installs) == 1
    assert terminal.prompts == [], "no Press Enter on success"
    assert terminal.reopened == [["ui", "--after-update"]]
    assert updates.take_press() is not None, "the record is left for the asq that reopens"


def test_one_click_installs_unpinned_and_leaves_the_receipt_free_to_move(
    runner: CliRunner,
    tool: Tool,  # noqa: F811
    machine: Machine,  # noqa: F811
    terminal: Terminal,  # noqa: F811
) -> None:
    """The ⬆ update installs as ``aisquare upgrade`` does: the newest at or above the release
    that runs, refreshed, never ``==`` or ``@`` a version. uv records the spec it was given in
    the receipt, so a pinned one would hold every later ``uv tool upgrade`` back (review of
    #301)."""
    result = runner.invoke(cli_app, ["upgrade", "--one-click"])
    installs = [argv for argv, _env, _stderr in machine.installs]

    assert result.exit_code == 0, result.output
    assert len(installs) == 1, installs
    argv = installs[0]
    spec = argv[-1]
    assert spec.startswith("aisquare-cli") and spec.endswith(">=0.9.0"), argv
    assert "==" not in spec and "@" not in spec
    assert argv[-3:-1] == ["--refresh-package", "aisquare-cli"], "a release minutes old is seen"
    assert "--version" not in ui_app.UPDATE_HAND_OFF


@pytest.mark.parametrize(
    ("fleet", "remote", "said", "advice"),
    [
        (
            ((), 0, "tmux did not answer"),
            None,
            "· the fleet's agents could not be counted (tmux did not answer); whatever runs "
            "keeps running",
            "make sure none run",
        ),
        (
            ((), 0, None),
            "aisquare remote serve on port 8",
            "· Remote is on (aisquare remote serve on port 8): it can fail while the install is "
            "replaced, and runs 0.9.0 until it is started again",
            "turn it off first",
        ),
    ],
    ids=["fleet-uncounted", "remote-on"],
)
def test_one_click_says_facts_and_advises_no_stop_whatever_it_finds(
    runner: CliRunner,
    tool: Tool,  # noqa: F811
    machine: Machine,  # noqa: F811
    terminal: Terminal,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    fleet: Any,
    remote: str | None,
    said: str,
    advice: str,
) -> None:
    """No shutdown, stop or turn-off advice under ``--one-click``, for a fleet that could not be
    counted or a Remote on elsewhere either; ``--yes`` still gives it (control)."""
    monkeypatch.setattr(lifecycle, "running_fleet", lambda: fleet)
    monkeypatch.setattr(lifecycle, "running_remote", lambda: remote)

    one_click = runner.invoke(cli_app, ["upgrade", "--one-click"])
    plain_yes = runner.invoke(cli_app, ["upgrade", "--yes"])

    assert one_click.exit_code == 0 and plain_yes.exit_code == 0, one_click.output
    assert said in one_click.stdout
    assert advice not in one_click.stdout and "shutdown" not in one_click.stdout
    assert advice in plain_yes.stdout


@pytest.mark.parametrize(
    ("latest", "said", "installs"),
    [
        ("0.9.1", "✗ the update is done, but asq could not be opened again", 1),
        ("0.9.0", "✗ nothing was installed, and asq could not be opened again", 0),
    ],
    ids=["installed", "nothing-newer"],
)
def test_a_reopen_that_cannot_start_says_what_the_run_did(
    runner: CliRunner,
    tool: Tool,  # noqa: F811
    machine: Machine,  # noqa: F811
    terminal: Terminal,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    latest: str,
    said: str,
    installs: int,
) -> None:
    """Only the exec into asq failed: said as that, with how to open asq, never as an upgrade
    error, and never a second attempt behind Enter. After an install the update is done;
    with nothing newer, nothing was installed, which "the update is done" told otherwise
    (review of #301, N2). The press's record goes with it: no asq took it, and a
    ``ui --after-update`` typed later restarted agents from it (tester's review of #301)."""
    machine.latest = LatestRelease(latest)
    updates.record_press([])

    def cannot(args: list[str]) -> None:
        terminal.reopened.append(list(args))
        raise OSError(8, "Exec format error")

    monkeypatch.setattr(selfcli, "exec_self", cannot)

    result = runner.invoke(cli_app, ["upgrade", "--one-click"])

    assert result.exit_code == 0, result.output
    assert len(machine.installs) == installs, "control: the run installed or did not"
    assert f"{said} ([Errno 8] Exec format error). Type `asq` to open it." in result.stderr, (
        result.stderr
    )
    assert "stopped on an error" not in result.stderr
    assert terminal.prompts == [] and terminal.reopened == [["ui", "--after-update"]]
    assert _state(updates.PRESSED_KEY) is None, "no record left behind"


def test_a_reopen_that_cannot_start_after_hooks_failed_says_the_update_is_done(
    runner: CliRunner,
    tool: Tool,  # noqa: F811
    machine: Machine,  # noqa: F811
    terminal: Terminal,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Review of #301, n5: the install worked, a hook site then failed to re-connect (exit 1),
    and the exec into asq failed. The run installed the update, so that is what is said, as
    after a clean install; it said "asq could not be opened again" alone."""
    _record(_hooked(tmp_path / "claude", tool.script))
    machine.connect_exit = 1
    updates.record_press([])

    def cannot(args: list[str]) -> None:
        terminal.reopened.append(list(args))
        raise OSError(8, "Exec format error")

    monkeypatch.setattr(selfcli, "exec_self", cannot)

    result = runner.invoke(cli_app, ["upgrade", "--one-click"])

    assert result.exit_code == 1, result.output
    assert len(machine.installs) == 1, "control: the update was installed"
    assert "✓ aisquare 0.9.1 (was 0.9.0)" in result.stdout
    said = "✗ the update is done, but asq could not be opened again ([Errno 8] Exec format error)"
    assert said in result.stderr, result.stderr


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
    """Staying in the shell, nothing reopens to take the press's record: it goes too."""
    machine.installer_exit = 1
    terminal.answer = KeyboardInterrupt()
    updates.record_press([])

    result = runner.invoke(cli_app, ["upgrade", "--one-click"])

    assert result.exit_code == 1
    assert terminal.prompts == [_ENTER] and terminal.reopened == []
    assert _state(updates.PRESSED_KEY) is None, "no record left behind"


@pytest.mark.parametrize("pressed", ["read-at-length", "days-before-the-run"])
def test_a_failed_one_clicks_reopen_renews_the_press_it_was_handed(
    runner: CliRunner,
    tool: Tool,  # noqa: F811
    machine: Machine,  # noqa: F811
    terminal: Terminal,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    pressed: str,
) -> None:
    """The ten minutes start again as the run hands over to the reopen. A failure read at
    "Press Enter" for longer than that left a record the reopen dropped as too old, and it
    said nothing of the agents (review of #301, N3): here the run began 11 minutes before
    the Enter, a second after the press. A record already days old when the run began is no
    press of this run's (``upgrade --one-click`` typed by hand), and is never renewed."""
    machine.installer_exit = 1
    if pressed == "read-at-length":
        read_for = timedelta(minutes=11)
        readings = [datetime.now(UTC) - read_for]  # the run's start; the Enter is read now

        class Clock(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> Any:
                return readings.pop(0) if readings else datetime.now(tz)

        monkeypatch.setattr(install_cli, "datetime", Clock)
        updates.record_press([], datetime.now(UTC) - read_for - timedelta(seconds=1))
    else:
        updates.record_press([], datetime.now(UTC) - timedelta(days=2))

    result = runner.invoke(cli_app, ["upgrade", "--one-click"])

    assert result.exit_code == 1 and terminal.prompts == [_ENTER], result.output
    assert terminal.reopened == [["ui", "--after-update"]], "control: the run handed over"
    taken = updates.take_press()
    assert (taken is not None) is (pressed == "read-at-length"), taken


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
