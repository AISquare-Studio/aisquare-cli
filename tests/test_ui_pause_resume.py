"""Pause and resume agents in asq: Settings ▸ Fleet's *Pause all…* / *Resume all…*, a project
card's *Pause agents…* / *Resume agents…*, and the same two on the project's Settings tab.

Each opens the one dialog Restart all and Stop all open (``views.fleet_bulk.ConfirmBulk``):
who, by project, a *Managers only* switch, Cancel focused. Confirmed, ``fleet_service.bulk``
runs off the UI thread with a toast per agent and one that sums it up. Driven as
``test_ui_fleet_bulk.py`` drives Settings ▸ Fleet: the listing scripted, the rows in the store,
the per-agent pause and restart stand-ins, and the ``fleet-paused`` signal recorded rather
than written.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, OptionList, Static

from aisquare.cli.ui.click_guard import GuardedButton
from aisquare.cli.ui.project_menu import ProjectMenu
from aisquare.cli.ui.views.fleet_bulk import TITLE, ConfirmBulk
from aisquare.cli.ui.views.project import ProjectView
from aisquare.cli.ui.views.settings_hub import SettingsHubView
from aisquare.models import FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service
from tests import toast_log
from tests.test_ui_fleet_bulk import (
    _ROWS,
    Acted,
    Script,
    _all_rows,
    _app,
    _confirm,
    _listed,
    _open,
    drive,
)
from tests.test_ui_fleet_bulk import no_tmux as _no_tmux
from tests.test_ui_fleet_bulk import script as _script
from tests.test_ui_project_cards import _menu
from tests.test_ui_shell import seed, settle, shown, status

#: Re-exported so pytest collects them here (the guard is autouse there, and here).
no_tmux = _no_tmux
script = _script


def _paused(project_id: str, label: str, role: str, *, minute: int = 0) -> FleetAgentStatus:
    """A scripted agent ``fleet pause`` stopped: its row ended as ``paused``."""
    running = status(project_id, label, role, "waiting", minute=minute)
    agent = running.agent.model_copy(
        update={
            "ended_at": running.agent.created_at + timedelta(minutes=1),
            "ended_by": "stopped",
            "paused_at": running.agent.created_at + timedelta(minutes=1),
        }
    )
    return FleetAgentStatus(agent=agent, state="paused")


class Paused(Acted):
    """:class:`Acted`, with the pause of one agent stood in for too, and each project's
    ``fleet-paused`` signal recorded as it is set or cleared."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        super().__init__(monkeypatch)
        self.signals: list[tuple[str, str]] = []
        monkeypatch.setattr(fleet_service, "pause_agent", self.pause_agent)
        monkeypatch.setattr(
            fleet_service, "pause", lambda p, **_: self.signals.append(("on", p.id))
        )
        monkeypatch.setattr(
            fleet_service, "resume", lambda p, **_: self.signals.append(("off", p.id))
        )

    def pause_agent(
        self, project: ProjectInfo, label: str, **kwargs: Any
    ) -> fleet_service.StopReceipt:
        self.calls.append(("pause", label, kwargs.get("agent_id")))
        self._wait()
        row = next(s.agent for s in _all_rows() if s.agent.id == kwargs.get("agent_id"))
        return fleet_service.StopReceipt(row, [], withheld=True)


@pytest.fixture
def running(tmp_path: Path, script: Script) -> Script:
    """alpha and beta, each with a manager and a coder at work; beta's tester exited."""
    seed(tmp_path, ("prj_a", "alpha", "amber-otter"), ("prj_b", "beta", "blue-heron"))
    script["prj_a"] = [
        status("prj_a", "manager", "manager", "waiting"),
        status("prj_a", "coder-1", "coder", "working", minute=1),
    ]
    script["prj_b"] = [
        status("prj_b", "manager", "manager", "working"),
        status("prj_b", "coder-1", "coder", "waiting", minute=1),
        status("prj_b", "tester-1", "tester", "exited", minute=2, exit_status=0),
    ]
    return script


@pytest.fixture
def paused(tmp_path: Path, script: Script) -> Script:
    """alpha paused, manager and coder; beta running, with a tester that exited unpaused."""
    seed(tmp_path, ("prj_a", "alpha", "amber-otter"), ("prj_b", "beta", "blue-heron"))
    script["prj_a"] = [
        _paused("prj_a", "manager", "manager"),
        _paused("prj_a", "coder-1", "coder", minute=1),
    ]
    script["prj_b"] = [
        status("prj_b", "manager", "manager", "working"),
        status("prj_b", "tester-1", "tester", "exited", minute=2, exit_status=0),
    ]
    return script


def _acting(monkeypatch: pytest.MonkeyPatch, rows: Script) -> Iterator[Paused]:
    _ROWS[:] = [row for listed in rows.values() for row in listed]
    yield Paused(monkeypatch)
    _ROWS.clear()


@pytest.fixture
def acting_on_running(monkeypatch: pytest.MonkeyPatch, running: Script) -> Iterator[Paused]:
    yield from _acting(monkeypatch, running)


@pytest.fixture
def acting_on_paused(monkeypatch: pytest.MonkeyPatch, paused: Script) -> Iterator[Paused]:
    yield from _acting(monkeypatch, paused)


# --- Settings ▸ Fleet --------------------------------------------------------------------


def test_settings_fleet_has_pause_all_and_resume_all_beside_restart_and_stop(
    running: Script,
) -> None:
    async def go(pilot: Pilot[None]) -> list[tuple[str, bool]]:
        hub = _app(pilot).query_one(SettingsHubView)
        return [
            (str(button.label), isinstance(button, GuardedButton))
            for button in hub.query(".fleet-action").results(Button)
        ]

    assert drive(go) == [
        ("Restart all…", True),
        ("Stop all…", True),
        ("Pause all…", True),
        ("Resume all…", True),
    ]


def test_pause_all_lists_who_by_project_with_cancel_focused(running: Script) -> None:
    async def go(pilot: Pilot[None]) -> tuple[list[str], str, str | None, str]:
        dialog = await _open(pilot, "pause")
        focused = _app(pilot).focused
        return (
            _listed(dialog),
            str(_confirm(dialog).label),
            focused.id if focused else None,
            shown(dialog.query_one("#bulk-title", Static)),
        )

    listed, label, focused, title = drive(go)
    assert listed == [
        "alpha",
        "  manager · manager · ⏸ waiting",
        "  coder-1 · coder · ▶ working",
        "",
        "beta",
        "  manager · manager · ▶ working",
        "  coder-1 · coder · ⏸ waiting",
        "  tester-1 · tester · 💤 exited — left: exited",
    ]
    assert label == "Pause 4 agents"
    assert focused == "bulk-cancel"
    assert title == TITLE["pause"]


def test_confirming_pause_all_pauses_each_agent_with_a_toast_and_sets_the_signals(
    acting_on_running: Paused,
) -> None:
    async def go(pilot: Pilot[None]) -> list[str]:
        app = _app(pilot)
        told = toast_log.record(app)
        dialog = await _open(pilot, "pause")
        _confirm(dialog).press()
        await settle(app)
        return told

    toasts = drive(go)
    assert [verb for verb, _, _ in acting_on_running.calls] == ["pause"] * 4
    assert acting_on_running.signals == [("on", "prj_a"), ("on", "prj_b")]
    assert [t for t in toasts if t.startswith("⏸ paused")] == [
        "⏸ paused manager (alpha)",
        "⏸ paused coder-1 (alpha)",
        "⏸ paused manager (beta)",
        "⏸ paused coder-1 (beta)",
    ]
    assert [t for t in toasts if t.startswith("paused ")] == [
        "paused 4 · skipped 1 · failed 0\nfleet-paused: alpha, beta\n"
        "skipped: tester-1 (beta) — exited"
    ]


def test_resume_all_lists_the_paused_agents_and_leaves_the_rest(acting_on_paused: Paused) -> None:
    async def go(pilot: Pilot[None]) -> tuple[list[str], str]:
        dialog = await _open(pilot, "resume")
        return _listed(dialog), str(_confirm(dialog).label)

    listed, label = drive(go)
    assert listed == [
        "alpha",
        "  coder-1 · coder · ⏯ paused — will be resumed",
        "  manager · manager · ⏯ paused — will be resumed",
        "",
        "beta",
        "  tester-1 · tester · 💤 exited — left: not paused",
        "  manager · manager · ▶ working — left: not paused",
    ]
    assert label == "Resume 2 agents"


# --- the card's menu and the Settings tab -------------------------------------------------


def _options(pilot: Pilot[None]) -> list[tuple[str | None, bool]]:
    screen = _app(pilot).screen
    assert isinstance(screen, ProjectMenu)
    options = screen.query_one(OptionList)
    return [
        (options.get_option_at_index(i).id, options.get_option_at_index(i).disabled)
        for i in range(options.option_count)
    ]


def test_the_card_menu_offers_pause_and_resume_greying_the_one_that_does_nothing(
    acting_on_paused: Paused,
) -> None:
    async def go(pilot: Pilot[None]) -> list[list[tuple[str | None, bool]]]:
        app = _app(pilot)
        seen = []
        for project_id in ("prj_a", "prj_b"):
            app.sidebar.open_menu(project_id)
            await settle(app)
            seen.append([o for o in _options(pilot) if o[0] in ("pause", "resume")])
            await pilot.press("escape")
            await settle(app)
        return seen

    alpha, beta = drive(go)
    assert alpha == [("pause", True), ("resume", False)], "alpha is paused"
    assert beta == [("pause", False), ("resume", True)], "beta runs, and nothing is paused"


def test_the_card_menus_resume_asks_for_that_project_and_resumes_it_alone(
    acting_on_paused: Paused,
) -> None:
    async def go(pilot: Pilot[None]) -> tuple[str, list[str], list[str]]:
        app = _app(pilot)
        told = toast_log.record(app)
        app.sidebar.open_menu("prj_a")
        await settle(app)
        await _menu(pilot, "resume")
        dialog = app.screen
        assert isinstance(dialog, ConfirmBulk)
        title = shown(dialog.query_one("#bulk-title", Static))
        listed = _listed(dialog)
        _confirm(dialog).press()
        await settle(app)
        return title, listed, told

    title, listed, toasts = drive(go)
    assert title == "Resume the paused agents of alpha?"
    assert listed == [
        "alpha",
        "  coder-1 · coder · ⏯ paused — will be resumed",
        "  manager · manager · ⏯ paused — will be resumed",
    ]
    assert [(verb, label) for verb, label, _ in acting_on_paused.calls] == [
        ("restart", "coder-1"),
        ("restart", "manager"),
    ]
    assert acting_on_paused.signals == [("off", "prj_a")]
    assert any(t.startswith("resumed 2 · skipped 0 · failed 0") for t in toasts)


def test_the_settings_tab_has_pause_and_resume_that_ask_for_that_project(
    acting_on_running: Paused,
) -> None:
    async def go(pilot: Pilot[None]) -> tuple[dict[str, bool], str]:
        app = _app(pilot)
        app.sidebar.open_menu("prj_a")
        await settle(app)
        await _menu(pilot, "settings")
        view = app.current_view()
        assert isinstance(view, ProjectView) and view.active == "tab-settings"
        greyed = {
            action: view.query_one(f"#{action}-agents", Button).disabled
            for action in ("pause", "resume")
        }
        view.query_one("#pause-agents", Button).press()
        await settle(app)
        dialog = app.screen
        assert isinstance(dialog, ConfirmBulk)
        return greyed, shown(dialog.query_one("#bulk-title", Static))

    greyed, title = drive(go)
    assert greyed == {"pause": False, "resume": True}, "nothing of alpha's is paused"
    assert title == "Pause the running agents of alpha?"
    assert acting_on_running.calls == [], "nothing is paused until it is confirmed"


def test_a_signal_with_no_agent_paused_is_cleared_from_the_card(
    acting_on_running: Paused, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #457, n3: a signal-only pause (an earlier version's), or one a ``--managers``
    resume left on, greyed *Resume agents…*, and asq had no way to clear it."""
    signalled = frozenset({"prj_a"})
    monkeypatch.setattr(fleet_service, "fleet_paused_projects", lambda store, projects: signalled)
    monkeypatch.setattr(fleet_service, "_paused_projects", lambda store, every: set(signalled))

    async def go(pilot: Pilot[None]) -> tuple[list[tuple[str | None, bool]], list[str], str]:
        app = _app(pilot)
        app.refresh_data()
        await settle(app)
        app.sidebar.open_menu("prj_a")
        await settle(app)
        options = [o for o in _options(pilot) if o[0] in ("pause", "resume")]
        await _menu(pilot, "resume")
        dialog = app.screen
        assert isinstance(dialog, ConfirmBulk)
        listed, label = _listed(dialog), str(_confirm(dialog).label)
        _confirm(dialog).press()
        await settle(app)
        return options, listed, label

    options, listed, label = drive(go)
    assert options == [("pause", False), ("resume", False)], "both act: alpha runs, and is paused"
    assert listed[0] == "no agent is paused; Resume clears the fleet-paused signal of alpha"
    assert label == "Clear fleet-paused"
    assert acting_on_running.signals == [("off", "prj_a")]
    assert acting_on_running.calls == [], "no agent was started or stopped"
