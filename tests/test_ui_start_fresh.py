"""Start fresh… in the fleet UI: on a project card's menu and on its Settings tab (#2 of the
Start fresh tasks; ``services.fresh`` is #1's, tested in ``test_project_fresh.py``).

The owner's words: a button, "available in two places: the project's menu in the left pane
… and the project's Settings view". Both open one dialog that reads ``services.fresh.plan``,
shows what is always cleared and that a new blank manager starts, and offers the three
extras, off; unpushed work asks a second time; a refused plan says why and cannot be
confirmed; and nothing is cleared until it is confirmed, by ``services.fresh.start_fresh``
off the UI thread.

Driven as ``test_ui_project_cards.py`` drives the menu: the real store in the isolated
home, ``list_agents`` scripted, the spawn of the new manager faked.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from textual.geometry import Region
from textual.pilot import Pilot
from textual.widgets import Button, Checkbox, OptionList, Static
from textual.widgets._toast import Toast
from textual.worker import WorkerState

from aisquare.cli.ui.app import FleetApp, _fresh_group
from aisquare.cli.ui.board import BoardPanel
from aisquare.cli.ui.fresh import EXTRAS, ConfirmDiscard, ConfirmFresh, FreshChoice, StartFresh
from aisquare.cli.ui.project_menu import ProjectMenu
from aisquare.cli.ui.views.project import ProjectView
from aisquare.core import paths
from aisquare.core.entries import new_entry
from aisquare.core.ids import new_event_id, new_task_id
from aisquare.core.store import store_session
from aisquare.models import (
    FleetAgent,
    FreshPlan,
    FreshUnpushed,
    ProjectInfo,
    TeamEvent,
    TeamTask,
)
from aisquare.services import fleet as fleet_service
from aisquare.services import fresh as fresh_service
from tests.test_ui_project_cards import _menu
from tests.test_ui_shell import (
    PRIVATE_SOCKET,
    Script,
    _toasts,
    drive,
    fleet_app,
    seed,
    settle,
    shown,
)
from tests.test_ui_shell import no_real_tmux as _no_real_tmux
from tests.test_ui_shell import script as _script

#: Re-exported so pytest collects them here (the guard is autouse there, and here).
no_real_tmux = _no_real_tmux
script = _script

NOW = datetime(2026, 10, 25, tzinfo=UTC)


def _board(project_id: str) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of ``project_id`` a Start fresh could clear, as stored."""
    connection = sqlite3.connect(str(paths.db_path()))
    try:
        return {
            table: connection.execute(
                f"SELECT * FROM {table} WHERE project_id = ? ORDER BY rowid", (project_id,)
            ).fetchall()
            for table in ("team_task", "team_event", "fleet_agent", "entry", "prompt")
        }
    finally:
        connection.close()


@pytest.fixture
def alpha(tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch) -> ProjectInfo:
    """Alpha onboarded, with two tasks, a note, a remembered fact and a prompt on it. No tmux
    session of its stands: ``fleet shutdown``'s plan, which the dialog's plan asks, would
    probe the fleet's socket, which a UI test never addresses."""
    monkeypatch.setattr(
        fleet_service, "shutdown_plan", lambda *_, **__: fleet_service.ShutdownPlan()
    )
    monkeypatch.setattr(fleet_service, "project_sessions", lambda *_, **__: [])
    (tmp_path / "alpha").mkdir()
    (project,) = seed(tmp_path, ("prj_a", "alpha", "amber-otter"))
    with store_session() as store:
        for n in range(2):
            store.upsert_task(
                TeamTask(
                    id=new_task_id(),
                    project_id=project.id,
                    key=f"t{n}",
                    title=f"task {n}",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        store.add_team_event(
            TeamEvent(id=new_event_id(), project_id=project.id, text="a note", created_at=NOW)
        )
        store.add(new_entry("alpha remembers", "project", project.id, [], "manual"))
        store.add_prompt("a prompt", project.id)
    return project


@pytest.fixture
def applied(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """``start_fresh`` as it runs, its keyword arguments recorded, with the new manager's
    spawn faked (a row written, as a spawn writes one)."""
    calls: list[dict[str, Any]] = []
    real = fresh_service.start_fresh

    def spawn(project: ProjectInfo, role: str, **_: Any) -> fleet_service.SpawnReceipt:
        with store_session() as store:
            row = store.upsert_fleet_agent(
                FleetAgent(
                    id="agt_a_fresh",
                    project_id=project.id,
                    label="manager",
                    role=role,
                    pane_id="%7",
                    tmux_socket=PRIVATE_SOCKET,
                    session_id="sess-new",
                    cwd=project.root,
                    created_at=NOW,
                )
            )
        return fleet_service.SpawnReceipt(agent=row, asked_label=None, tmux_session="asq-x")

    def start_fresh(project: ProjectInfo, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return real(project, **kwargs)

    monkeypatch.setattr(fleet_service, "spawn", spawn)
    monkeypatch.setattr(fresh_service, "start_fresh", start_fresh)
    return calls


async def _open_from_menu(pilot: Pilot[None]) -> ConfirmFresh:
    app = fleet_app(pilot)
    app.sidebar.open_menu("prj_a")
    await settle(app)
    await _menu(pilot, "fresh")
    assert isinstance(app.screen, ConfirmFresh), app.screen
    return app.screen


def _plan_text(dialog: ConfirmFresh) -> str:
    return shown(dialog.query_one("#fresh-plan", Static))


async def _tick(pilot: Pilot[None], dialog: ConfirmFresh, checkbox: str) -> str:
    dialog.query_one(f"#{checkbox}", Checkbox).toggle()
    await settle(fleet_app(pilot))
    return _plan_text(dialog)


def test_the_menu_lists_start_fresh_just_above_remove(alpha: ProjectInfo) -> None:
    async def go(pilot: Pilot[None]) -> list[tuple[str | None, str]]:
        app = fleet_app(pilot)
        app.sidebar.open_menu("prj_a")
        await settle(app)
        assert isinstance(app.screen, ProjectMenu)
        options = app.screen.query_one(OptionList)
        return [
            (options.get_option_at_index(i).id, str(options.get_option_at_index(i).prompt))
            for i in range(options.option_count)
        ]

    listed = drive(go)
    assert [option_id for option_id, _ in listed][-2:] == ["fresh", "remove"]
    assert ("fresh", "↺ Start fresh…") in listed


def test_the_menu_and_the_settings_button_open_the_same_dialog(alpha: ProjectInfo) -> None:
    async def go(pilot: Pilot[None]) -> list[str]:
        app = fleet_app(pilot)
        opened: list[str] = []
        dialog = await _open_from_menu(pilot)
        opened.append(f"{type(dialog).__name__}:{dialog.project.id}")
        await pilot.press("escape")
        await settle(app)
        app.sidebar.open_menu("prj_a")
        await settle(app)
        await _menu(pilot, "settings")
        view = app.current_view()
        assert isinstance(view, ProjectView) and view.active == "tab-settings"
        view.query_one("#start-fresh", Button).press()
        await settle(app)
        screen = app.screen
        assert isinstance(screen, ConfirmFresh)
        opened.append(f"{type(screen).__name__}:{screen.project.id}")
        return opened

    assert drive(go) == ["ConfirmFresh:prj_a", "ConfirmFresh:prj_a"]


def test_the_dialog_shows_the_plan_and_each_extra_starts_off_and_changes_it(
    alpha: ProjectInfo,
) -> None:
    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)
        dialog = await _open_from_menu(pilot)
        await settle(app)
        seen: dict[str, object] = {
            "focused": app.focused.id if app.focused else None,
            "ticked": [box.value for box in dialog.query(Checkbox)],
            "labels": [str(box.label) for box in dialog.query(Checkbox)],
            "plain": _plan_text(dialog),
            "confirm": dialog.query_one("#fresh-confirm", Button).disabled,
        }
        seen["memory"] = await _tick(pilot, dialog, "fresh-memory")
        seen["prompts"] = await _tick(pilot, dialog, "fresh-prompts")
        seen["branches"] = await _tick(pilot, dialog, "fresh-branches")
        seen["memory off"] = await _tick(pilot, dialog, "fresh-memory")
        return seen

    seen = drive(go)
    assert seen["focused"] == "fresh-cancel"
    assert seen["ticked"] == [False, False, False]
    assert seen["labels"] == ["Project memory", "Prompt log & metrics", "Fleet git branches"]
    plain = " ".join(str(seen["plain"]).split())
    assert "Always cleared: 2 tasks · 1 note or event · 0 sessions" in plain
    assert "0 fleet agents · 0 worktrees" in plain
    assert "Then a new blank manager starts." in plain and "Also cleared" not in plain
    assert seen["confirm"] is False
    assert "Also cleared: 1 context entry" in " ".join(str(seen["memory"]).split())
    assert "1 context entry · 1 prompt · 0 turn metrics" in " ".join(str(seen["prompts"]).split())
    assert "0 turn metrics · 0 fleet branches" in " ".join(str(seen["branches"]).split())
    off = " ".join(str(seen["memory off"]).split())
    assert "context entry" not in off and "Also cleared: 1 prompt" in off


def test_cancel_and_escape_leave_the_store_untouched(
    alpha: ProjectInfo, applied: list[dict[str, Any]]
) -> None:
    before = _board("prj_a")

    async def go(pilot: Pilot[None]) -> list[object]:
        app = fleet_app(pilot)
        await _open_from_menu(pilot)
        await settle(app)
        await pilot.press("enter")  # Cancel has the focus: an Enter too fast clears nothing
        await settle(app)
        after_enter = type(app.screen).__name__
        dialog = await _open_from_menu(pilot)
        await settle(app)
        await _tick(pilot, dialog, "fresh-memory")
        await pilot.press("escape")
        await settle(app)
        return [after_enter, type(app.screen).__name__]

    screens = drive(go)
    assert "ConfirmFresh" not in screens
    assert _board("prj_a") == before and applied == []


def test_confirm_applies_with_the_extras_chosen_and_says_what_was_cleared(
    alpha: ProjectInfo, applied: list[dict[str, Any]]
) -> None:
    async def go(pilot: Pilot[None]) -> str:
        app = fleet_app(pilot)
        dialog = await _open_from_menu(pilot)
        await settle(app)
        await _tick(pilot, dialog, "fresh-memory")
        dialog.query_one("#fresh-confirm", Button).press()
        return await _toasts(app)

    toasts = drive(go, notifications=True)
    assert applied == [{"memory": True, "prompts": False, "branches": False, "discard": ()}]
    assert "✓ alpha started fresh — cleared 2 tasks, 1 note or event" in toasts, toasts
    assert "1 context entry; a new manager started (session sess-new)" in toasts, toasts
    assert "manager manager" not in toasts
    board = _board("prj_a")
    assert board["team_task"] == board["team_event"] == board["entry"] == []
    assert len(board["prompt"]) == 1, "the prompt log stays without its box ticked"
    assert [row[0] for row in board["fleet_agent"]] == ["agt_a_fresh"]


def test_unpushed_work_asks_again_and_cancelling_there_applies_nothing(
    alpha: ProjectInfo,
    applied: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    tree = tmp_path / "trees" / "coder-1"
    real_plan = fresh_service.plan

    def plan(project: ProjectInfo, **kwargs: Any) -> FreshPlan:
        found = real_plan(project, **kwargs)
        held = [
            FreshUnpushed(path=tree, branch="fleet/amber-otter/coder-1", commits=2),
            FreshUnpushed(branch="fleet/amber-otter/old", commits=1),
        ]
        return found.model_copy(update={"unpushed": held})

    monkeypatch.setattr(fresh_service, "plan", plan)
    before = _board("prj_a")

    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)
        dialog = await _open_from_menu(pilot)
        await settle(app)
        warned = " ".join(_plan_text(dialog).split())
        dialog.query_one("#fresh-confirm", Button).press()
        await settle(app)
        asked = app.screen
        assert isinstance(asked, ConfirmDiscard), asked
        listing = shown(asked.query_one("#discard-list", Static))
        focused = app.focused.id if app.focused else None
        await pilot.press("enter")  # Cancel has the focus
        await settle(app)
        back = type(app.screen).__name__
        cancelled = list(applied)
        branch_only = await _tick(pilot, dialog, "fresh-branches")
        dialog.query_one("#fresh-confirm", Button).press()
        await settle(app)
        second = app.screen
        assert isinstance(second, ConfirmDiscard)
        rows = shown(second.query_one("#discard-list", Static))
        second.query_one("#discard-confirm", Button).press()
        await settle(app)
        return {
            "warned": warned,
            "listing": listing,
            "focused": focused,
            "back": back,
            "cancelled": cancelled,
            "branch_only": branch_only,
            "rows": rows,
        }

    seen = drive(go)
    assert "1 worktree or branch holds work no remote has" in str(seen["warned"])
    assert str(seen["listing"]) == (
        f"fleet/amber-otter/coder-1 — 2 commits on no remote · uncommitted: no — {tree}"
    ), "a branch no tree has out is not at risk without Fleet git branches"
    assert seen["focused"] == "discard-cancel"
    assert seen["back"] == "ConfirmFresh" and seen["cancelled"] == []
    assert "2 worktrees or branches hold work" in " ".join(str(seen["branch_only"]).split())
    assert "fleet/amber-otter/old — 1 commit on no remote · uncommitted: no — no worktree" in str(
        seen["rows"]
    )
    # The discard covers the two places the dialog listed, at the tips it listed, and no more.
    assert [{k: v for k, v in call.items() if k != "discard"} for call in applied] == [
        {"memory": False, "prompts": False, "branches": True}
    ]
    assert [(item.branch, item.commits) for item in applied[0]["discard"]] == [
        ("fleet/amber-otter/coder-1", 2),
        ("fleet/amber-otter/old", 1),
    ]
    assert _board("prj_a") != before


def test_a_refused_plan_says_why_and_cannot_be_confirmed(
    alpha: ProjectInfo, applied: list[dict[str, Any]], tmp_path: Path
) -> None:
    seed(tmp_path, ("prj_b", "beta", "bold-heron"))
    with store_session() as store:
        store.upsert_fleet_agent(
            FleetAgent(
                id="agt_b_manager",
                project_id="prj_b",
                label="manager",
                role="manager",
                pane_id="%3",
                tmux_socket=PRIVATE_SOCKET,
                cwd=tmp_path / "beta",
                board="prj_a",
                created_at=NOW,
            )
        )
    before = _board("prj_a")

    async def go(pilot: Pilot[None]) -> tuple[str, bool]:
        app = fleet_app(pilot)
        dialog = await _open_from_menu(pilot)
        await settle(app)
        confirm = dialog.query_one("#fresh-confirm", Button)
        disabled = confirm.disabled
        await _tick(pilot, dialog, "fresh-memory")
        return _plan_text(dialog), disabled and confirm.disabled

    said, disabled = drive(go)
    said = " ".join(said.split())
    assert (
        "can't start fresh: alpha is the board beta's 1 live fleet agent(s) read: manager" in said
    )
    assert disabled
    assert _board("prj_a") == before and applied == []


# --- reopened at 8b66e535 (tester-fresh-ui) --------------------------------------------------


def _feed(panel: BoardPanel) -> list[str]:
    feed = panel.query_one("#feed", OptionList)
    return [str(feed.get_option_at_index(i).prompt) for i in range(feed.option_count)]


def _post(project_id: str, text: str) -> None:
    with store_session() as store:
        store.add_team_event(
            TeamEvent(
                id=new_event_id(), project_id=project_id, text=text, created_at=datetime.now(UTC)
            )
        )


@pytest.mark.parametrize("hidden", [False, True], ids=["board shown", "board hidden"])
def test_the_boards_feed_holds_only_what_was_written_after_the_reset(
    alpha: ProjectInfo, applied: list[dict[str, Any]], hidden: bool
) -> None:
    """The feed reads past its cursor and only grows, so after a reset it kept every cleared
    note, signal and ``agent_exited``, selectable, until asq restarted: on a board open
    through it, on one hidden then shown, and after ``r``. Every open board of the project
    starts over (``BoardPanel.start_over``)."""
    _post("prj_a", "before the reset")

    async def go(pilot: Pilot[None]) -> dict[str, list[str]]:
        app = fleet_app(pilot)
        app.sidebar.open_menu("prj_a")
        await settle(app)
        await _menu(pilot, "settings")
        view = app.current_view()
        assert isinstance(view, ProjectView)
        view.active = "tab-board"
        await settle(app)
        panel = view.query_one(BoardPanel)
        seen = {"before": _feed(panel)}
        if hidden:
            view.active = "tab-settings"
            await settle(app)
        dialog = await _open_from_menu(pilot)
        await settle(app)
        dialog.query_one("#fresh-confirm", Button).press()
        await settle(app)
        _post("prj_a", "after the reset")
        view.active = "tab-board"
        await settle(app)
        panel.query_one("#feed", OptionList).focus()  # the panel's keys, as a click there gives
        await pilot.press("r")
        await settle(app)
        seen["after"] = _feed(panel)
        seen["ids"] = list(panel._events_by_id)
        return seen

    seen = drive(go)
    assert any("before the reset" in line for line in seen["before"])
    assert [line for line in seen["after"] if "the reset" in line] == [
        line for line in seen["after"] if "after the reset" in line
    ]
    assert not any("before the reset" in line or "a note" in line for line in seen["after"])
    assert any("after the reset" in line for line in seen["after"])
    connection = sqlite3.connect(str(paths.db_path()))
    try:
        stored = {row[0] for row in connection.execute("SELECT id FROM team_event")}
    finally:
        connection.close()
    assert set(seen["ids"]) <= stored, "no event the reset deleted is selectable"


def _unpushed_plan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every line the dialog can show: live agents, a session, every extra, unpushed work."""
    real_plan = fresh_service.plan

    def plan(project: ProjectInfo, **kwargs: Any) -> FreshPlan:
        found = real_plan(project, **kwargs)
        return found.model_copy(
            update={
                "live_agents": ["manager", "coder-auth", "coder-billing"],
                "sessions": ["asq:asq-amber-otter"],
                "unpushed": [
                    FreshUnpushed(
                        path=tmp_path / "trees" / f"coder-{n}",
                        branch=f"fleet/amber-otter/coder-{n}",
                        commits=n,
                        uncommitted=bool(n % 2),
                    )
                    for n in range(1, 4)
                ],
            }
        )

    monkeypatch.setattr(fresh_service, "plan", plan)


def _inside(inner: Region, outer: Region) -> bool:
    return (
        outer.x <= inner.x
        and outer.y <= inner.y
        and inner.right <= outer.right
        and inner.bottom <= outer.bottom
    )


def test_both_dialogs_fit_whole_at_80x24(
    alpha: ProjectInfo,
    applied: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """At 80x24, ConfirmDiscard (84 wide) lost its right border and Cancel's edge, and
    ConfirmFresh with every extra ticked was a row too tall: its buttons lost their bottom
    edge. Each box fits the screen, and its buttons fit inside its border; the text
    scrolls instead."""
    _unpushed_plan(monkeypatch, tmp_path)

    async def go(pilot: Pilot[None]) -> list[tuple[str, Region, Region, list[Region]]]:
        app = fleet_app(pilot)
        dialog = await _open_from_menu(pilot)
        await settle(app)
        for checkbox, _ in EXTRAS:
            await _tick(pilot, dialog, checkbox)
        boxes = []
        box = dialog.query_one("#fresh-box")
        buttons = [b.region for b in dialog.query(Button)]
        boxes.append(("fresh", app.screen.region, box.region, buttons))
        dialog.query_one("#fresh-confirm", Button).press()
        await settle(app)
        discard = app.screen
        assert isinstance(discard, ConfirmDiscard)
        box = discard.query_one("#discard-box")
        buttons = [b.region for b in discard.query(Button)]
        boxes.append(("discard", app.screen.region, box.region, buttons))
        return boxes

    for name, screen, box, buttons in drive(go, size=(80, 24)):
        assert _inside(box, screen), (name, box, screen)
        border = box.shrink((1, 1, 1, 1))  # the heavy border is one cell all round
        assert buttons and all(_inside(button, border) for button in buttons), (name, box, buttons)


def test_a_late_unpushed_refusal_says_what_to_do_in_the_ui(
    alpha: ProjectInfo,
    applied: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Work committed after the dialog read its plan: the run plans again and refuses. Its
    toast told a UI user to "pass --discard-unpushed"; it says to open Start fresh again."""
    later = fresh_service.plan(alpha).model_copy(
        update={"unpushed": [FreshUnpushed(path=tmp_path / "t", branch="fleet/x/c", commits=1)]}
    )

    def refuse(project: ProjectInfo, **_: Any) -> Any:
        raise fresh_service.FreshRefused(
            fresh_service.unpushed_refusal(later), code="unpushed_work", plan=later
        )

    monkeypatch.setattr(fresh_service, "start_fresh", refuse)

    async def go(pilot: Pilot[None]) -> tuple[str, str, list[bool]]:
        app = fleet_app(pilot)
        dialog = await _open_from_menu(pilot)
        await settle(app)
        await _tick(pilot, dialog, "fresh-prompts")
        dialog.query_one("#fresh-confirm", Button).press()
        toasts = await _toasts(app)
        await settle(app)
        again = app.screen
        ticked = [box.value for box in again.query(Checkbox)]
        return toasts, type(again).__name__, ticked

    toasts, again, ticked = drive(go, notifications=True)
    assert "confirm the discard in Start fresh…, open again on it" in toasts, toasts
    assert "fleet/x/c" in toasts and "--discard-unpushed" not in toasts
    # The dialog opens again on what the run found, ticked as the run was asked.
    assert again == "ConfirmFresh" and ticked == [False, True, False]


def test_start_fresh_is_refused_while_a_run_of_it_is_going(
    alpha: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Start fresh could be opened again, and confirmed, while a run was in flight: two runs
    would each stop what the other spawned and start a manager. The menu, the Settings
    button and a confirmation are each refused while it runs, and the worker is exclusive in
    its project's group."""
    release = threading.Event()
    runs: list[str] = []

    def start_fresh(project: ProjectInfo, **_: Any) -> Any:
        runs.append(project.id)
        assert release.wait(30), "the test never released the run"
        raise fresh_service.FreshRefused("stood in for a run", code="fleet_error")

    monkeypatch.setattr(fresh_service, "start_fresh", start_fresh)

    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)
        dialog = await _open_from_menu(pilot)
        await settle(app)
        dialog.query_one("#fresh-confirm", Button).press()
        for _ in range(20):  # not settle: it would wait for the held run
            await pilot.pause()
            if runs:
                break
        group = _fresh_group("prj_a")
        running = [w for w in app.workers if w.group == group and not w.is_finished]
        app.post_message(StartFresh("prj_a"))  # the menu's and the Settings button's
        for _ in range(5):
            await pilot.pause()
        reopened = type(app.screen).__name__
        app._start_fresh(FreshChoice("prj_a"))  # a confirmation landing meanwhile
        for _ in range(5):
            await pilot.pause()
        release.set()
        toasts = await _toasts(app)
        return {"running": len(running), "reopened": reopened, "toasts": toasts}

    seen = drive(go, notifications=True)
    assert seen["running"] == 1
    assert seen["reopened"] != "ConfirmFresh"
    assert str(seen["toasts"]).count("alpha is starting fresh already") == 2
    assert runs == ["prj_a"], "one run, however often it was asked for"


def test_a_second_start_never_cancels_a_reset_in_flight(
    alpha: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #429, round 2: the worker was ``exclusive`` in its project's group, which
    guards nothing. It cancels the run in flight, whose thread goes on, and drops its report.
    ``_fresh_running`` is the guard; with it out of the way here, a second start leaves the
    first running, and both report."""
    release = threading.Event()
    runs: list[str] = []

    def start_fresh(project: ProjectInfo, **_: Any) -> Any:
        runs.append(project.id)
        assert release.wait(30), "the test never released the run"
        raise fresh_service.FreshRefused(f"stood in for run {len(runs)}", code="fleet_error")

    monkeypatch.setattr(fresh_service, "start_fresh", start_fresh)
    monkeypatch.setattr(FleetApp, "_fresh_running", lambda self, project: False)

    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)
        app._start_fresh(FreshChoice("prj_a"))
        for _ in range(20):  # not settle: it would wait for the held run
            await pilot.pause()
            if runs:
                break
        group = _fresh_group("prj_a")
        first = next(w for w in app.workers if w.group == group)
        app._start_fresh(FreshChoice("prj_a"))
        for _ in range(5):
            await pilot.pause()
        state = first.state
        release.set()
        toasts = await _toasts(app)
        return {"state": state, "toasts": toasts}

    seen = drive(go, notifications=True)
    assert seen["state"] is not WorkerState.CANCELLED
    assert str(seen["toasts"]).count("stood in for run") == 2, seen["toasts"]


@pytest.mark.parametrize("noted", [True, False], ids=["sweep noted it", "sweep said nothing"])
def test_the_toast_says_the_tmux_session_ended_with_its_last_window(
    alpha: ProjectInfo,
    applied: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    noted: bool,
) -> None:
    """The common case: every pane a live agent, so stopping them closes the session's last
    window and tmux ends it. ``kill-session`` never runs and ``sessions_killed`` stays empty,
    so the toast said nothing of the session (tester-fresh-ui at a14eafc1). A session the
    plan listed that the stop neither killed nor failed on has ended, noted absent by the
    sweep or not."""
    with store_session() as store:
        live = store.upsert_fleet_agent(
            FleetAgent(
                id="agt_a_manager",
                project_id="prj_a",
                label="manager",
                role="manager",
                pane_id="%2",
                tmux_socket=PRIVATE_SOCKET,
                cwd=alpha.root,
                created_at=NOW,
            )
        )
    session = "asq:asq-amber-otter"
    standing = [session]
    monkeypatch.setattr(fleet_service, "project_sessions", lambda *_: list(standing))
    monkeypatch.setattr(
        fleet_service,
        "shutdown_plan",
        lambda *_, **__: fleet_service.ShutdownPlan(agents=[live], sessions=[session]),
    )

    def shutdown(project: ProjectInfo, **_: Any) -> fleet_service.ShutdownReport:
        with store_session() as store:  # its window closed, and the session with it
            store.end_fleet_agent(live.id, exit_status=0, ended_by="stopped")
        standing.clear()
        return fleet_service.ShutdownReport(
            stopped=[live], sessions_absent=[session] if noted else []
        )

    monkeypatch.setattr(fleet_service, "shutdown", shutdown)

    async def go(pilot: Pilot[None]) -> str:
        app = fleet_app(pilot)
        dialog = await _open_from_menu(pilot)
        await settle(app)
        dialog.query_one("#fresh-confirm", Button).press()
        return await _toasts(app)

    toasts = drive(go, notifications=True)
    assert "; its tmux session ended; a new manager started (session sess-new)" in toasts, toasts


def test_a_refusal_toast_names_three_places_and_counts_the_rest_at_80x24(
    alpha: ProjectInfo,
    applied: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """tester-fresh-ui: at 80x24 a refusal toast listing several long paths was taller than
    the screen, and its title and what to do scrolled off. It names three places and counts
    the rest, and fits."""
    deep = tmp_path / ("a-rather-long-folder-name-" * 3)
    later = fresh_service.plan(alpha).model_copy(
        update={
            "unpushed": [
                FreshUnpushed(path=deep / f"coder-{n}", branch=f"fleet/x/coder-{n}", commits=1)
                for n in range(1, 7)
            ]
        }
    )

    def refuse(project: ProjectInfo, **_: Any) -> Any:
        raise fresh_service.FreshRefused("refused", code="unpushed_work", plan=later)

    monkeypatch.setattr(fresh_service, "start_fresh", refuse)

    async def go(pilot: Pilot[None]) -> tuple[str, list[int]]:
        app = fleet_app(pilot)
        dialog = await _open_from_menu(pilot)
        await settle(app)
        dialog.query_one("#fresh-confirm", Button).press()
        toasts = await _toasts(app)
        heights = [toast.region.height for toast in app.query(Toast)]
        return toasts, heights

    toasts, heights = drive(go, notifications=True, size=(80, 24))
    assert "confirm the discard in Start fresh…, open again on it" in toasts
    assert "coder-3" in toasts and "coder-4" not in toasts and "…and 3 more" in toasts
    assert heights and max(heights) < 24, heights
