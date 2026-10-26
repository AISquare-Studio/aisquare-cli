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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, Checkbox, OptionList, Static

from aisquare.cli.ui.fresh import ConfirmDiscard, ConfirmFresh
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
    assert applied == [
        {"memory": True, "prompts": False, "branches": False, "discard_unpushed": False}
    ]
    assert "✓ alpha started fresh — cleared 2 tasks, 1 note or event" in toasts, toasts
    assert "1 context entry; new manager manager started" in toasts, toasts
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
    assert applied == [
        {"memory": False, "prompts": False, "branches": True, "discard_unpushed": True}
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
