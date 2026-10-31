"""An agent's row renames it in place: a second click, or F2, and the label is a field.

The owner's words: the manager says "coder-1 is blocked and needs your review", and a
spawn-time label means nothing to them; a name they chose ("login fix") does. The row
mirrors the project card's rename: the first click opens the agent (as it always did), a
second click at the same spot opens a field over the label, prefilled and all selected, with
the label it will become under it. Enter or a click away saves, Esc cancels, a frame never
touches what is being typed, and the write runs in a worker: ``fleet relabel``'s own service
function, the one the CLI calls.

Driven as ``test_ui_shell.py`` drives the shell — the real store in the isolated home,
``list_agents`` scripted, every click sent as the terminal sends it — with
``fleet_service.relabel`` replaced by a recorder, so what is asked of the service, and on
which thread, is what is read.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from textual import events
from textual.geometry import Offset
from textual.pilot import Pilot
from textual.widget import Widget
from textual.widgets import Static
from textual.widgets._toast import Toast

from aisquare.cli.ui import app as app_mod
from aisquare.cli.ui.app import FleetApp, HelpScreen
from aisquare.cli.ui.sidebar import (
    RENAME_TIP,
    AgentLabelInput,
    AgentRow,
    NicknameInput,
    ProjectTitle,
    relabel_preview,
)
from aisquare.cli.ui.terminal import TerminalPane
from aisquare.cli.ui.views.agent import AgentView
from aisquare.models import FleetAgent, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services.fleet import FleetError, RelabelReceipt
from tests.test_ui_shell import (
    Script,
    _as_the_terminal_sends,
    _click,
    _toasts,
    card_for,
    drive,
    fleet_app,
    highlighted,
    refreshed,
    row_for,
    seed,
    settle,
    shown,
    status,
)
from tests.test_ui_shell import no_real_tmux as _no_real_tmux
from tests.test_ui_shell import script as _script
from tests.ui_workers import watch_until

#: Re-exported so pytest collects them here (the guard is autouse there, and here).
no_real_tmux = _no_real_tmux
script = _script

CODER = "agt_a_coder-1"
MANAGER = "agt_a_manager"


@pytest.fixture(autouse=True)
def wide_click_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two clicks at one cell always chain, however loaded the runner (as
    ``test_ui_project_cards.py`` holds the card's double-click): the row's own rule decides."""
    monkeypatch.setattr(FleetApp, "CLICK_CHAIN_TIME_THRESHOLD", 5.0)


@dataclass
class Relabels:
    """What the sidebar asked ``fleet_service.relabel``, on which thread, and what it answers."""

    calls: list[tuple[str, str, str, dict[str, Any]]] = field(default_factory=list)
    threads: list[int] = field(default_factory=list)
    answer: RelabelReceipt | Exception | None = None
    hold: threading.Event | None = None


@pytest.fixture
def relabels(monkeypatch: pytest.MonkeyPatch) -> Relabels:
    seen = Relabels()

    def fake(project: ProjectInfo, label: str, text: str, **kwargs: Any) -> RelabelReceipt:
        seen.calls.append((project.id, label, text, kwargs))
        seen.threads.append(threading.get_ident())
        if seen.hold is not None:
            assert seen.hold.wait(10), "the test never let the rename finish"
        if isinstance(seen.answer, Exception):
            raise seen.answer
        if seen.answer is not None:
            return seen.answer
        agent = FleetAgent(
            id=CODER,
            project_id=project.id,
            label=fleet_service.label_from_text(text, "coder"),
            role="coder",
            pane_id="%1",
            cwd=Path("/w"),
            created_at=status("prj_a", "coder-1", "coder", "working").agent.created_at,
        )
        return RelabelReceipt(agent, label, agent.label, [])

    monkeypatch.setattr(fleet_service, "relabel", fake)
    return seen


def _team(script: Script, tmp_path: Path) -> None:
    seed(tmp_path, ("prj_a", "alpha", None), ("prj_b", "beta", None))
    script["prj_a"] = [
        status("prj_a", "manager", "manager", "waiting"),
        status("prj_a", "coder-1", "coder", "working", minute=1),
    ]


async def _double_click(pilot: Pilot[None], row: AgentRow) -> None:
    """Two clicks, and — when the second opened the name field — the field holding the keys.

    The field takes them after the next frame is drawn (``AgentRow.open_rename``), and on
    Windows a pending Textual timer holds one of asyncio's executor threads, so that frame can
    come after ``settle`` has returned, and what the test types next would go to the sidebar.
    Watched for as the toasts are (``watch_until``: no thread, a bounded wait)."""
    app = fleet_app(pilot)
    for kind in (events.MouseDown, events.MouseUp, events.MouseDown, events.MouseUp):
        await _as_the_terminal_sends(pilot, kind, row)
    await settle(app)
    await watch_until(lambda: not row.renaming or app.focused is row.field)


async def _keys_to_the_sidebar(app: FleetApp) -> None:
    """Hand the keys to the sidebar, as the escape key does, once an agent row's click has
    handed them to the agent's pane.

    The pane takes them after the frame that shows it is drawn (``FleetApp._focus_pane``),
    which on Windows can be after ``settle``: landing after the sidebar's focus, it would take
    the F2 that follows. Every row clicked here opens a pane, an ended agent's too."""
    await watch_until(lambda: isinstance(app.focused, TerminalPane))
    app.sidebar.focus()
    await watch_until(lambda: app.focused is app.sidebar)


async def _f2_opens(pilot: Pilot[None], target: Callable[[], Widget | None]) -> None:
    """F2, and the field it opens holding the keys: ``target()`` is the one to wait for.

    The field is mounted at once, but it takes the focus after the next frame is drawn: read
    at once, the agent's F2 found no field holding the keys on windows-py3.12 (rc/0.9.2's CI,
    run 41080458606, shard 7/10)."""
    app = fleet_app(pilot)
    await pilot.press("f2")

    def holds_the_keys() -> bool:
        opened = target()
        return opened is not None and opened.display and app.focused is opened

    await watch_until(holds_the_keys)


async def _f2_refused(pilot: Pilot[None], why: str) -> None:
    """F2 on a row that offers no rename, and its toast (``why`` in it) up before anything is
    read: the toast is the sign the key reached the sidebar at all."""
    app = fleet_app(pilot)
    await pilot.press("f2")
    await watch_until(lambda: any(why in toast.render().plain for toast in app.screen.query(Toast)))


def _field(app: FleetApp, agent_id: str = CODER) -> AgentLabelInput | None:
    return row_for(app, agent_id).field


def _preview(app: FleetApp, agent_id: str = CODER) -> str:
    return shown(row_for(app, agent_id).query_one(".agent-label-preview", Static))


# --- the label it will become ---------------------------------------------------------------


def test_the_preview_is_the_label_the_rename_will_make() -> None:
    coder = status("prj_a", "coder-1", "coder", "working").agent
    assert relabel_preview("Login Fix", coder).plain == "→ login-fix"
    assert relabel_preview("2FA work", coder).plain == "→ coder-2fa-work"
    assert relabel_preview("Coder 1", coder).plain == "→ coder-1 (as it is)"
    taken = relabel_preview("login fix", coder, {"login-fix", "manager"}).plain
    assert taken == "→ login-fix-2 (login-fix is taken)"
    assert relabel_preview("!!!", coder).plain == "✗ needs 2 letters or digits"
    assert relabel_preview("Manager", coder).plain == "✗ manager is reserved"


# --- the gesture ------------------------------------------------------------------------------


def test_a_second_click_on_the_row_renames_the_agent_and_enter_saves_in_a_worker(
    tmp_path: Path, script: Script, relabels: Relabels
) -> None:
    _team(script, tmp_path)

    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)
        row = row_for(app, CODER)
        await _double_click(pilot, row)
        field = _field(app)
        assert field is not None, "the second click opened the field"
        opened = {
            "view": isinstance(app.current_view(), AgentView),
            "value": field.value,
            "selected": field.selected_text,
            "focused": app.focused is field,
            "preview": _preview(app),
            "tall": row.size.height,
        }
        await pilot.press(*"Login", "space", *"fix")
        typed = _preview(app)
        await pilot.press("enter")
        await settle(app)
        return {
            **opened,
            "typed": typed,
            "closed": _field(app) is None and row.size.height == 1,
            "sidebar_focused": app.focused is app.sidebar,
            "toasts": await _toasts(app),
        }

    seen = drive(go, notifications=True)
    assert seen["view"], "the first click of the two opened the agent, as it always did"
    assert seen["value"] == seen["selected"] == "coder-1", "prefilled, all selected"
    assert seen["focused"] and seen["preview"] == "→ coder-1 (as it is)"
    assert seen["tall"] == 2, "the field and the label it will become, one line each"
    assert seen["typed"] == "→ login-fix"
    assert seen["closed"] and seen["sidebar_focused"]
    assert relabels.calls == [("prj_a", "coder-1", "Login fix", {"actor": "user"})]
    assert relabels.threads and relabels.threads[0] != threading.main_thread().ident
    assert "coder-1 is now login-fix" in str(seen["toasts"])


def test_a_zero_gap_double_click_still_opens_the_field(
    tmp_path: Path, script: Script, relabels: Relabels
) -> None:
    """Press, release, press, release in one write, as a fast hand or a trackpad's tap-tap
    sends them: the second click opens the field while the first one's view is still being
    shown, and the view must not take the keys from it once it is (``_focus_pane``)."""
    _team(script, tmp_path)

    async def go(pilot: Pilot[None]) -> tuple[bool, bool, bool]:
        app = fleet_app(pilot)
        row = row_for(app, CODER)
        x, y = row.region.offset + Offset(1, 0)
        for kind in (events.MouseDown, events.MouseUp, events.MouseDown, events.MouseUp):
            app.post_message(kind(None, x, y, 0, 0, 1, False, False, False, screen_x=x, screen_y=y))
        await settle(app)
        await pilot.pause()
        await settle(app)
        field = _field(app)
        return (
            isinstance(app.current_view(), AgentView),
            field is not None,
            field is not None and app.focused is field,
        )

    view, opened, focused = drive(go)
    assert view, "the first click opened the agent"
    assert opened and focused, "the second click's field is open and keeps the keys"
    assert relabels.calls == [], "nothing saved by a blur"


def test_the_preview_follows_a_frame_that_takes_the_label(
    tmp_path: Path, script: Script, relabels: Relabels
) -> None:
    """``→ login-fix`` while it is free; a frame that brings a live ``login-fix`` says
    ``→ login-fix-2`` at once, with nothing typed since."""
    _team(script, tmp_path)

    async def go(pilot: Pilot[None]) -> tuple[str, str, str]:
        app = fleet_app(pilot)
        await _double_click(pilot, row_for(app, CODER))
        await pilot.press(*"login", "space", *"fix")
        before = _preview(app)
        script["prj_a"] = [
            *script["prj_a"],
            status("prj_a", "login-fix", "tester", "working", minute=2),
        ]
        await refreshed(app)
        field = _field(app)
        assert field is not None
        return before, _preview(app), field.value

    before, after, typed = drive(go)
    assert before == "→ login-fix"
    assert after == "→ login-fix-2 (login-fix is taken)"
    assert typed == "login fix", "the field itself untouched"


def test_the_rename_never_blocks_the_ui_while_it_writes(
    tmp_path: Path, script: Script, relabels: Relabels
) -> None:
    _team(script, tmp_path)
    hold = relabels.hold = threading.Event()

    async def go(pilot: Pilot[None]) -> tuple[bool, str | None, str | None]:
        app = fleet_app(pilot)
        await _double_click(pilot, row_for(app, CODER))
        await pilot.press(*"login", "enter")
        for _ in range(3):
            await pilot.pause()
        writing = any(
            worker.name == app_mod.AGENT_RENAME_WORKER and not worker.is_finished
            for worker in app.workers
        )
        before = app.sidebar.selected_key
        await pilot.press("up")  # the UI answers keys while the write waits
        moved = app.sidebar.selected_key
        hold.set()
        await settle(app)
        return writing, before, moved

    writing, before, moved = drive(go)
    assert writing, "the write was still in its worker"
    assert moved != before, "the sidebar moved its highlight meanwhile"


def test_f2_renames_the_highlighted_agent_and_the_card_title_still_renames_the_project(
    tmp_path: Path, script: Script, relabels: Relabels
) -> None:
    _team(script, tmp_path)

    async def go(pilot: Pilot[None]) -> tuple[bool, str, bool, bool]:
        app = fleet_app(pilot)
        await _click(pilot, row_for(app, CODER))
        await _keys_to_the_sidebar(app)
        await _f2_opens(pilot, lambda: _field(app))
        field = _field(app)
        agent_open = field is not None and app.focused is field
        value = field.value if field is not None else ""
        await pilot.press(*"nope", "escape")  # Esc: closed, nothing written
        await settle(app)
        cancelled = _field(app) is None and app.focused is app.sidebar
        await _click(pilot, card_for(app, "prj_a").query_one(ProjectTitle))
        app.sidebar.focus()
        await watch_until(lambda: app.focused is app.sidebar)
        await _f2_opens(pilot, lambda: card_for(app, "prj_a").query_one(NicknameInput))
        nickname = card_for(app, "prj_a").query_one(NicknameInput)
        return agent_open, value, cancelled, nickname.display and app.focused is nickname

    agent_open, value, cancelled, project_open = drive(go)
    assert agent_open and value == "coder-1"
    assert cancelled and relabels.calls == []
    assert project_open, "F2 on a card title renames the project, as before"


def test_the_renamed_row_keeps_the_one_highlight_and_the_arrows_go_on_from_it(
    tmp_path: Path, script: Script, relabels: Relabels
) -> None:
    """The rename meets the sidebar's one highlight (#458): F2 on the highlighted row, Enter,
    and the frame that brings the new label — the row, still the agent's, keeps the highlight
    and the keys, and ↑/↓ go on from it, opening the row they land on."""
    _team(script, tmp_path)

    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)
        await _click(pilot, row_for(app, CODER))
        await _keys_to_the_sidebar(app)
        await _f2_opens(pilot, lambda: _field(app))
        await pilot.press(*"login", "space", *"fix", "enter")
        await settle(app)
        manager, coder = script["prj_a"]
        renamed = coder.agent.model_copy(update={"label": "login-fix"})
        script["prj_a"] = [manager, coder.model_copy(update={"agent": renamed})]
        await refreshed(app)
        seen: dict[str, object] = {
            "after": highlighted(app),
            "label": row_for(app, CODER).status.agent.label,
            "keys": app.focused is app.sidebar,
        }
        await pilot.press("up")
        await settle(app)
        seen["up"] = (highlighted(app), str(app.content.current))
        await pilot.press("down")
        await settle(app)
        seen["down"] = (highlighted(app), str(app.content.current))
        return seen

    seen = drive(go)
    assert relabels.calls == [("prj_a", "coder-1", "login fix", {"actor": "user"})]
    assert seen["after"] == [f"agent:{CODER}"] and seen["label"] == "login-fix"
    assert seen["keys"], "Enter handed the keys back to the sidebar"
    assert seen["up"] == ([f"agent:{MANAGER}"], f"agent-{MANAGER}")
    assert seen["down"] == ([f"agent:{CODER}"], f"agent-{CODER}"), "on from the renamed row"


def test_leaving_the_field_saves_and_a_frame_never_touches_what_is_typed(
    tmp_path: Path, script: Script, relabels: Relabels
) -> None:
    _team(script, tmp_path)

    async def go(pilot: Pilot[None]) -> tuple[str, bool, str, bool]:
        app = fleet_app(pilot)
        await _double_click(pilot, row_for(app, CODER))
        await pilot.press(*"auth", "space", *"wor")
        # A frame lands mid-word: the row's state changes, the field stays as typed.
        script["prj_a"] = [
            status("prj_a", "manager", "manager", "working"),
            status("prj_a", "coder-1", "coder", "waiting", minute=1),
        ]
        await refreshed(app)
        field = _field(app)
        assert field is not None
        kept, focused = field.value, app.focused is field
        await pilot.press("k")
        # A click away saves, as Enter does.
        await _click(pilot, card_for(app, "prj_b").query_one(ProjectTitle))
        return kept, focused, row_for(app, CODER).status.state, _field(app) is None

    kept, focused, state, closed = drive(go)
    assert kept == "auth wor" and focused, "the frame repainted the row, not the field"
    assert state == "waiting", "the frame did land"
    assert closed and relabels.calls == [("prj_a", "coder-1", "auth work", {"actor": "user"})]


def test_a_refused_name_reopens_the_field_with_what_was_typed(
    tmp_path: Path, script: Script, relabels: Relabels
) -> None:
    _team(script, tmp_path)
    relabels.answer = FleetError("cannot rename 'coder-1' to 'manager': that label is reserved")

    async def go(pilot: Pilot[None]) -> tuple[str, str | None, bool]:
        app = fleet_app(pilot)
        await _double_click(pilot, row_for(app, CODER))
        await pilot.press(*"Manager", "enter")
        await settle(app)
        toasts = await _toasts(app)
        field = _field(app)
        return toasts, field.value if field is not None else None, app.focused is field

    toasts, value, focused = drive(go, notifications=True)
    assert "that label is reserved" in toasts and "not renamed" in toasts
    assert value == "Manager" and focused, "reopened with what was typed, to correct"


def test_the_managers_row_offers_no_rename_and_f2_says_why(
    tmp_path: Path, script: Script, relabels: Relabels
) -> None:
    _team(script, tmp_path)
    script["prj_a"].append(status("prj_a", "tester-1", "tester", "exited", minute=2, exit_status=0))

    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)
        manager = row_for(app, MANAGER)
        await _double_click(pilot, manager)
        double = _field(app, MANAGER) is None
        await _keys_to_the_sidebar(app)
        await _f2_refused(pilot, "the manager is always labelled 'manager'")
        f2 = _field(app, MANAGER) is None
        await _click(pilot, row_for(app, "agt_a_tester-1"))
        await _keys_to_the_sidebar(app)
        await _f2_refused(pilot, "tester-1 has ended")
        return {
            "double": double,
            "f2": f2,
            "ended": _field(app, "agt_a_tester-1") is None,
            "tips": (
                manager.tooltip,
                row_for(app, CODER).tooltip,
                row_for(app, "agt_a_tester-1").tooltip,
            ),
            "toasts": await _toasts(app),
        }

    seen = drive(go, notifications=True)
    assert seen["double"] and seen["f2"] and seen["ended"], "no field on either row"
    assert seen["tips"] == (None, RENAME_TIP, None), "only the coder's row offers it"
    toasts = str(seen["toasts"])
    assert "not renamed: the manager is always labelled 'manager'" in toasts
    assert "not renamed: tester-1 has ended — only a running agent is renamed" in toasts
    assert relabels.calls == []


def test_the_tooltip_the_second_click_and_f2_ask_one_rule(
    tmp_path: Path, script: Script, relabels: Relabels, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fleet_service.relabel_refusal`` decides all three: the tooltip never offers what a
    second click or F2 would refuse."""
    _team(script, tmp_path)
    monkeypatch.setattr(
        fleet_service,
        "relabel_refusal",
        lambda agent: "refused for this test" if agent.id == CODER else None,
    )

    async def go(pilot: Pilot[None]) -> tuple[object, bool, bool, str]:
        app = fleet_app(pilot)
        row = row_for(app, CODER)
        tip = row.tooltip
        await _double_click(pilot, row)
        double = _field(app) is None
        await _keys_to_the_sidebar(app)
        await _f2_refused(pilot, "refused for this test")
        return tip, double, _field(app) is None, await _toasts(app)

    tip, double, f2, toasts = drive(go, notifications=True)
    assert tip is None, "no rename offered"
    assert double and f2, "neither gesture opens a field"
    assert "not renamed: refused for this test" in toasts
    assert relabels.calls == []


def test_one_field_at_a_time_across_cards_and_rows(
    tmp_path: Path, script: Script, relabels: Relabels
) -> None:
    _team(script, tmp_path)

    async def go(pilot: Pilot[None]) -> tuple[bool, bool, bool]:
        app = fleet_app(pilot)
        await _click(pilot, card_for(app, "prj_b").query_one(ProjectTitle))
        app.sidebar.open_rename("prj_b")
        await settle(app)
        app.sidebar.open_agent_rename(CODER)
        await settle(app)
        card_closed = not card_for(app, "prj_b").query_one(NicknameInput).display
        agent_open = _field(app) is not None
        app.sidebar.open_rename("prj_a")
        await settle(app)
        return card_closed, agent_open, _field(app) is None

    card_closed, agent_open, agent_closed = drive(go)
    assert card_closed and agent_open and agent_closed
    assert relabels.calls == [], "a field closed by another opening saves nothing"


def test_the_help_screen_names_the_agent_rename(tmp_path: Path, script: Script) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> str:
        app = fleet_app(pilot)
        app.sidebar.focus()
        await pilot.press("question_mark")
        await settle(app)
        assert isinstance(app.screen, HelpScreen)
        return shown(app.screen.query_one(Static))

    keys = drive(go)
    assert "rename the card in place" in keys
    assert "on an agent's row, rename the agent (its label and tmux window)" in keys
