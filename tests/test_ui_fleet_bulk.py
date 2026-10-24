"""Settings ▸ Fleet: *Restart all…* and *Stop all…* in asq, through the real app.

User feedback (2026-10-19): four managers, and a restart of all of them was a click into
each, while clicking around between panes had destroyed a manager more than once. The
listing is scripted per project (``fleet_service.list_agents``), and so is the one look at
each agent just before its turn (``fleet_service.status_of``), with the scripted rows in
the store, so the dialog's plan and the action's second read are the service's own
(``bulk_plan``, ``bulk``); only the stop and the restart of one agent are stand-ins, which
record the row they were asked for.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, Static, Switch

from aisquare.cli.ui import sidebar
from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.click_guard import GuardedButton
from aisquare.cli.ui.sidebar import SettingsRow
from aisquare.cli.ui.views.fleet_bulk import BULK_WORKER, TITLE, ConfirmBulk
from aisquare.cli.ui.views.settings_hub import SettingsHubView
from aisquare.core import tmux as tmux_core
from aisquare.core.store import store_session
from aisquare.core.tmux import Completed
from aisquare.models import FleetAgent, FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service
from tests import toast_log
from tests.test_ui_shell import seed, settle, shown, status
from tests.ui_workers import settle_page

T = TypeVar("T")
SIZE = (140, 44)


class Script(dict[str, list[FleetAgentStatus]]):
    """What ``fleet_service.list_agents`` answers, per project id — mutable mid-test. The rows
    of each list set are recorded in the store, where ``bulk`` reads each row again."""

    def __setitem__(self, project_id: str, statuses: list[FleetAgentStatus]) -> None:
        super().__setitem__(project_id, statuses)
        with store_session() as store:
            for scripted in statuses:
                store.upsert_fleet_agent(scripted.agent)


@pytest.fixture(autouse=True)
def no_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    """No tmux command reaches a server: nothing here mounts a pane, and the listing is
    scripted, so a call that gets this far is a failure the test reads, never a fleet."""

    def refuse(argv: Sequence[str], stdin: bytes | None) -> Completed:
        return Completed(1, "", "no server running (a UI test addresses no real fleet)\n")

    monkeypatch.setattr(tmux_core, "_tmux", refuse)


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> Script:
    """What ``fleet_service.list_agents`` answers, per project id, and ``status_of`` for one
    of those rows (``lost`` for a row no list holds now) — mutable mid-test."""
    agents = Script()

    def fake(project: ProjectInfo, *, live_only: bool = True, **kwargs: Any) -> list[Any]:
        return list(agents.get(project.id, []))

    def one(agent: FleetAgent) -> FleetAgentStatus:
        listed = (status for rows in agents.values() for status in rows)
        found = next((status for status in listed if status.agent.id == agent.id), None)
        return found or FleetAgentStatus(agent=agent, state="lost")

    monkeypatch.setattr(fleet_service, "list_agents", fake)
    monkeypatch.setattr(fleet_service, "status_of", one)
    return agents


@pytest.fixture
def two_projects(tmp_path: Path, script: Script) -> Script:
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


class Acted:
    """The stand-ins for the stop and restart of one agent: who they were asked for, and
    an optional gate that holds each until a test lets it go."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.gate: threading.Event | None = None
        monkeypatch.setattr(fleet_service, "stop", self.stop)
        monkeypatch.setattr(fleet_service, "restart", self.restart)

    def _wait(self) -> None:
        if self.gate is not None:
            assert self.gate.wait(10), "the test never let the action go"

    def stop(self, project: ProjectInfo, label: str, **kwargs: Any) -> fleet_service.StopReceipt:
        self.calls.append(("stop", label, kwargs.get("agent_id")))
        self._wait()
        row = next(s.agent for s in _all_rows() if s.agent.id == kwargs.get("agent_id"))
        return fleet_service.StopReceipt(row, [])

    def restart(
        self, project: ProjectInfo, label: str, **kwargs: Any
    ) -> fleet_service.RestartReceipt:
        self.calls.append(("restart", label, kwargs.get("agent_id")))
        self._wait()
        row = next(s.agent for s in _all_rows() if s.agent.id == kwargs.get("agent_id"))
        started = row.model_copy(update={"id": f"{row.id}_new"})
        return fleet_service.RestartReceipt(
            replaced=row, started=started, resumed=True, was_running=True,
            tmux_session="asq-test",
        )  # fmt: skip


_ROWS: list[FleetAgentStatus] = []


def _all_rows() -> list[FleetAgentStatus]:
    return _ROWS


@pytest.fixture
def acted(monkeypatch: pytest.MonkeyPatch, two_projects: Script) -> Iterator[Acted]:
    _ROWS[:] = [row for rows in two_projects.values() for row in rows]
    yield Acted(monkeypatch)
    _ROWS.clear()


def drive(fn: Callable[[Pilot[None]], Awaitable[T]]) -> T:
    """``fn`` against a mounted ``FleetApp`` with toasts on, on the Settings hub."""

    async def run() -> T:
        app = FleetApp(refresh_seconds=3600, doctor=lambda: [])
        async with app.run_test(size=SIZE, notifications=True) as pilot:
            await pilot.pause()
            await settle(app)
            await pilot.click(app.query_one(SettingsRow))
            await settle(app)
            return await fn(pilot)

    return asyncio.run(run())


def _app(pilot: Pilot[None]) -> FleetApp:
    assert isinstance(pilot.app, FleetApp)
    return pilot.app


async def _open(pilot: Pilot[None], action: str) -> ConfirmBulk:
    """Press the hub's button for ``action`` (a key press; the guard is for the mouse) and
    let the dialog read its plan."""
    app = _app(pilot)
    app.query_one(f"#fleet-{action}-all", Button).press()
    await settle(app)
    dialog = app.screen
    assert isinstance(dialog, ConfirmBulk)
    return dialog


def _listed(dialog: ConfirmBulk) -> list[str]:
    return shown(dialog.query_one("#bulk-list", Static)).splitlines()


def _confirm(dialog: ConfirmBulk) -> Button:
    return dialog.query_one("#bulk-confirm", Button)


def test_the_sidebar_counts_alive_the_agents_stop_and_all_act_on() -> None:
    """Review of #266, round 3: the sidebar's "agents alive" roll-up kept a third copy of the
    set ``PROCESS_STATES`` and the agent view's Stop share. A state added to one and not
    the other, the sidebar and the dialog disagreed on how many agents run."""
    assert sidebar.ALIVE_STATES is fleet_service.PROCESS_STATES


def test_settings_has_a_fleet_section_with_the_two_guarded_buttons(two_projects: Script) -> None:
    async def go(pilot: Pilot[None]) -> tuple[list[tuple[str, bool]], str]:
        hub = _app(pilot).query_one(SettingsHubView)
        buttons = [
            (str(hub.query_one(f"#fleet-{action}-all", Button).label), isinstance(
                hub.query_one(f"#fleet-{action}-all", Button), GuardedButton
            ))
            for action in ("restart", "stop")
        ]  # fmt: skip
        return buttons, shown(hub.query_one("#hub-fleet-title", Static))

    buttons, title = drive(go)
    assert title == "Fleet"
    assert buttons == [("Restart all…", True), ("Stop all…", True)]


@pytest.mark.parametrize(
    ("action", "confirm"), [("restart", "Restart 4 agents"), ("stop", "Stop 4 agents")]
)
def test_each_dialog_lists_its_agents_by_project_with_cancel_focused(
    two_projects: Script, action: str, confirm: str
) -> None:
    async def go(pilot: Pilot[None]) -> tuple[list[str], str, bool, str | None, str]:
        dialog = await _open(pilot, action)
        focused = _app(pilot).focused
        return (
            _listed(dialog),
            str(_confirm(dialog).label),
            _confirm(dialog).disabled,
            focused.id if focused else None,
            shown(dialog.query_one("#bulk-title", Static)),
        )

    listed, label, disabled, focused, title = drive(go)
    if action == "stop":
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
    else:
        # A restart brings each manager back after its team (#38), and leaves an agent that
        # exited 0, which ended by itself.
        assert listed == [
            "alpha",
            "  coder-1 · coder · ▶ working",
            "  manager · manager · ⏸ waiting",
            "",
            "beta",
            "  coder-1 · coder · ⏸ waiting",
            f"  tester-1 · tester · 💤 exited — left: {fleet_service.ENDED_CLEANLY}",
            "  manager · manager · ▶ working",
        ]
    assert (label, disabled) == (confirm, False)
    assert focused == "bulk-cancel", "an Enter that came too fast acts on nobody"
    assert title == TITLE["restart" if action == "restart" else "stop"]


def test_the_managers_only_switch_redraws_the_list_and_the_count(two_projects: Script) -> None:
    async def go(pilot: Pilot[None]) -> list[tuple[list[str], str]]:
        dialog = await _open(pilot, "restart")
        seen = []
        switch = dialog.query_one("#bulk-managers", Switch)
        for value in (True, False):
            switch.value = value
            await settle(_app(pilot))
            seen.append((_listed(dialog), str(_confirm(dialog).label)))
        return seen

    managers, everyone = drive(go)
    assert managers == (
        [
            "alpha",
            "  manager · manager · ⏸ waiting",
            "",
            "beta",
            "  manager · manager · ▶ working",
        ],
        "Restart 2 managers",
    )
    assert everyone[1] == "Restart 4 agents" and len(everyone[0]) == 8


def test_the_confirm_button_is_as_wide_as_the_count_it_names(
    two_projects: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tester-pr266-ui, real UI: the button was laid out for "Restart" while the plan was
    read, and a new label is not a layout change in Textual, so "Restart 2 managers" read
    "Restart 2" (and "Restart 4 agents" did, or not, by which came first). Here the read
    is held until the dialog has been laid out, as it was there."""
    read = threading.Event()
    real = fleet_service.bulk_plan

    def held(*args: Any, **kwargs: Any) -> fleet_service.BulkPlan:
        assert read.wait(10), "the test never let the plan be read"
        return real(*args, **kwargs)

    monkeypatch.setattr(fleet_service, "bulk_plan", held)

    async def go(pilot: Pilot[None]) -> list[tuple[str, int]]:
        app = _app(pilot)
        app.query_one("#fleet-restart-all", Button).press()
        await settle_page(app, group="nothing-runs-here")
        dialog = app.screen
        assert isinstance(dialog, ConfirmBulk) and str(_confirm(dialog).label) == "Restart"
        read.set()
        seen = []
        for value in (False, True):
            dialog.query_one("#bulk-managers", Switch).value = value
            await settle(app)
            confirm = _confirm(dialog)
            seen.append((str(confirm.label), confirm.content_size.width))
        return seen

    seen = drive(go)
    assert [label for label, _ in seen] == ["Restart 4 agents", "Restart 2 managers"]
    for label, width in seen:
        assert width >= len(label), f"{label!r} is cut to {width} cells"


def test_escape_and_cancel_close_the_dialog_and_act_on_nobody(acted: Acted) -> None:
    async def go(pilot: Pilot[None]) -> list[bool]:
        app = _app(pilot)
        closed = []
        await _open(pilot, "stop")
        await pilot.press("escape")
        await settle(app)
        closed.append(not isinstance(app.screen, ConfirmBulk))
        dialog = await _open(pilot, "restart")
        dialog.query_one("#bulk-cancel", Button).press()
        await settle(app)
        closed.append(not isinstance(app.screen, ConfirmBulk))
        return closed

    assert drive(go) == [True, True]
    assert acted.calls == []


def test_a_click_on_confirm_as_the_dialog_appears_leaves_cancel_the_focus(acted: Acted) -> None:
    """Review of #266, round 2: the click the guard ignored still took the focus off
    Cancel on its mouse-down, and the Enter after it ran Stop all."""

    async def go(pilot: Pilot[None]) -> tuple[str | None, bool]:
        app = _app(pilot)
        dialog = await _open(pilot, "stop")
        dialog.query_one("#bulk-confirm", GuardedButton).changed_under_pointer()
        await pilot.click(_confirm(dialog))
        await settle(app)
        focused = app.focused.id if app.focused else None
        await pilot.press("enter")
        await settle(app)
        return focused, isinstance(app.screen, ConfirmBulk)

    focused, still_open = drive(go)
    assert focused == "bulk-cancel"
    assert not still_open, "the Enter went to Cancel"
    assert acted.calls == []


def test_nothing_running_says_so_and_offers_nothing_to_confirm(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, ("prj_a", "alpha", "amber-otter"))
    script["prj_a"] = [status("prj_a", "coder-1", "coder", "exited", exit_status=0)]

    async def go(pilot: Pilot[None]) -> tuple[list[str], str, bool]:
        dialog = await _open(pilot, "stop")
        return _listed(dialog), str(_confirm(dialog).label), _confirm(dialog).disabled

    listed, label, disabled = drive(go)
    assert listed[0] == "nothing to stop"
    assert "  coder-1 · coder · 💤 exited — left: exited" in listed
    assert (label, disabled) == ("Stop", True)


def test_confirming_runs_the_worker_with_the_buttons_greyed_until_the_summary(
    acted: Acted,
) -> None:
    """The UI never blocks: the action runs in a thread, both Fleet buttons are greyed
    while it does, each stopped agent gets its toast, and one toast sums it up."""
    acted.gate = threading.Event()

    async def go(pilot: Pilot[None]) -> tuple[list[bool], list[bool], list[str]]:
        app = _app(pilot)
        told = toast_log.record(app)
        hub = app.query_one(SettingsHubView)
        dialog = await _open(pilot, "stop")
        _confirm(dialog).press()
        # Settled on a group no worker has, as the bulk worker is held on purpose.
        await settle_page(app, group="nothing-runs-here")
        assert any(w.name == BULK_WORKER and not w.is_finished for w in app.workers)
        greyed = [hub.query_one(f"#fleet-{a}-all", Button).disabled for a in ("restart", "stop")]
        acted.gate.set()  # type: ignore[union-attr]
        await settle(app)
        after = [hub.query_one(f"#fleet-{a}-all", Button).disabled for a in ("restart", "stop")]
        return greyed, after, told

    greyed, after, toasts = drive(go)
    assert greyed == [True, True] and after == [False, False]
    assert [label for verb, label, _ in acted.calls] == ["manager", "coder-1"] * 2
    assert {verb for verb, _, _ in acted.calls} == {"stop"}
    assert [t for t in toasts if t.startswith("✓ stopped")] == [
        "✓ stopped manager (alpha)",
        "✓ stopped coder-1 (alpha)",
        "✓ stopped manager (beta)",
        "✓ stopped coder-1 (beta)",
    ]
    summary = [t for t in toasts if t.startswith("stopped ")]
    assert summary == ["stopped 4 · skipped 1 · failed 0\nskipped: tester-1 (beta) — exited"]


def test_a_quit_is_refused_while_the_run_goes_on_and_quits_once_it_ended(acted: Acted) -> None:
    """Review of #266, round 3: ``q`` mid-run tore the screen down and left the terminal
    blank while the thread stopped or restarted the rest of the agents, minutes for a
    restart of all, with every toast after the quit lost. Update and Uninstall refused it."""
    acted.gate = threading.Event()

    async def go(pilot: Pilot[None]) -> tuple[int | None, list[str], int | None]:
        app = _app(pilot)
        told = toast_log.record(app)
        dialog = await _open(pilot, "stop")
        _confirm(dialog).press()
        await settle_page(app, group="nothing-runs-here")  # the bulk worker is held
        app.sidebar.focus()
        await pilot.press("q")
        await settle_page(app, group="nothing-runs-here")
        refused = app.return_code
        toasts = [t for t in told if "still running" in t]
        acted.gate.set()  # type: ignore[union-attr]
        await settle(app)
        await pilot.press("q")
        await pilot.pause()
        return refused, toasts, app.return_code

    refused, toasts, quit_after = drive(go)
    assert refused is None, "asq quit under a run of every agent"
    assert toasts == [
        "Settings ▸ Fleet's restart or stop of every agent is still running — quit when it ends"
    ]
    assert quit_after == 0


def test_a_restart_of_all_is_told_per_agent_and_never_moves_the_view(acted: Acted) -> None:
    async def go(pilot: Pilot[None]) -> tuple[str | None, list[str]]:
        app = _app(pilot)
        told = toast_log.record(app)
        dialog = await _open(pilot, "restart")
        dialog.query_one("#bulk-managers", Switch).value = True
        await settle(app)
        _confirm(dialog).press()
        await settle(app)
        return app.content.current, told

    current, toasts = drive(go)
    assert current == "settings-hub", "the user's view stays where they put it"
    assert [(verb, label, agent_id) for verb, label, agent_id in acted.calls] == [
        ("restart", "manager", "agt_a_manager"),
        ("restart", "manager", "agt_b_manager"),
    ]
    assert "✓ restarted manager (alpha) — resumed its session" in toasts
    assert "✓ restarted manager (beta) — resumed its session" in toasts
    assert "restarted 2 · skipped 0 · failed 0" in toasts


def test_confirm_acts_on_the_agents_shown_and_skips_one_that_ended_since(
    acted: Acted, two_projects: Script
) -> None:
    """The plan is read again at confirm: alpha's coder exited while the dialog was open,
    so it is skipped as already exited, and beta's late coder was never shown, so it is
    left alone."""

    async def go(pilot: Pilot[None]) -> list[str]:
        app = _app(pilot)
        told = toast_log.record(app)
        dialog = await _open(pilot, "stop")
        two_projects["prj_a"][1] = status("prj_a", "coder-1", "coder", "exited", exit_status=0)
        two_projects["prj_b"].append(status("prj_b", "coder-2", "coder", "working", minute=3))
        _confirm(dialog).press()
        await settle(app)
        return told

    toasts = drive(go)
    assert [agent_id for _, _, agent_id in acted.calls] == [
        "agt_a_manager",
        "agt_b_manager",
        "agt_b_coder-1",
    ]
    [summary] = [t for t in toasts if t.startswith("stopped ")]
    assert summary.splitlines() == [
        "stopped 3 · skipped 2 · failed 0",
        "skipped: coder-1 (alpha) — already exited; tester-1 (beta) — exited",
    ]


def test_a_failing_agent_is_toasted_and_named_in_the_summary_and_the_rest_go_on(
    acted: Acted, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = acted.stop

    def failing(project: ProjectInfo, label: str, **kwargs: Any) -> fleet_service.StopReceipt:
        if kwargs.get("agent_id") == "agt_a_coder-1":
            raise fleet_service.FleetError("tmux refused the kill")
        return real(project, label, **kwargs)

    monkeypatch.setattr(fleet_service, "stop", failing)

    async def go(pilot: Pilot[None]) -> list[str]:
        app = _app(pilot)
        told = toast_log.record(app)
        dialog = await _open(pilot, "stop")
        _confirm(dialog).press()
        await settle(app)
        return told

    toasts = drive(go)
    assert "could not stop coder-1 (alpha): tmux refused the kill" in toasts
    assert "✓ stopped coder-1 (beta)" in toasts
    [summary] = [t for t in toasts if t.startswith("stopped ")]
    assert summary.splitlines() == [
        "stopped 3 · skipped 1 · failed 1",
        "skipped: tester-1 (beta) — exited",
        "failed: coder-1 (alpha) — tmux refused the kill",
    ]


def _ended(scripted: FleetAgentStatus, how: str) -> FleetAgentStatus:
    """``scripted``'s row, ended a minute ago ``how`` (``FleetAgent.ended_by``, #38): within
    the days a Restart all brings a dead agent back for."""
    ended = datetime.now(UTC) - timedelta(minutes=1)
    agent = scripted.agent.model_copy(update={"ended_by": how, "ended_at": ended})
    return scripted.model_copy(update={"agent": agent})


def test_restart_all_brings_back_an_agent_that_died_and_leaves_one_stopped_on_purpose(
    acted: Acted, two_projects: Script
) -> None:
    """#38, acceptance 2: an outside SIGTERM killed every agent (exit 143) and Restart all
    listed them as exited and restarted none. Driven in the real app: the dead coder is
    listed apart, "will be started again", the count says how many are dead, and Confirm
    restarts it; the coder ``fleet stop`` ended is left, and says why."""
    two_projects["prj_a"] = [
        *two_projects["prj_a"],
        _ended(status("prj_a", "coder-2", "coder", "exited", minute=2, exit_status=143), "died"),
    ]
    two_projects["prj_b"] = [
        *two_projects["prj_b"],
        _ended(status("prj_b", "coder-2", "coder", "exited", minute=3, exit_status=143), "stopped"),
    ]
    _ROWS[:] = [row for rows in two_projects.values() for row in rows]

    async def go(pilot: Pilot[None]) -> tuple[list[str], str]:
        app = _app(pilot)
        dialog = await _open(pilot, "restart")
        listed, label = _listed(dialog), str(_confirm(dialog).label)
        _confirm(dialog).press()
        await settle(app)
        return listed, label

    listed, label = drive(go)
    assert "  coder-2 · coder · 💤 exited (143) — will be started again" in listed
    assert f"  coder-2 · coder · 💤 exited — left: {fleet_service.STOPPED_ON_PURPOSE}" in listed
    assert label == "Restart 5 agents (4 running, 1 dead)"
    restarted = [agent_id for verb, _, agent_id in acted.calls if verb == "restart"]
    assert "agt_a_coder-2" in restarted, "the agent that died is started again"
    assert "agt_b_coder-2" not in restarted, "the agent stopped on purpose is left"
