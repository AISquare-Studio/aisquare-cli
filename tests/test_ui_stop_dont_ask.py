"""The Stop dialog's *Don't ask again* (``[ui] confirm_stop``), driven through the whole app.

Ticked at a Stop or a Force, the dialog saves ``confirm_stop = false``: the app holds it at
once, App settings' switch reads off, and it outlives a restart. From then on the agent
view's Stop and the sidebar's ``x`` stop at once — the dialog's own Stop (``/exit``, never
Force), off the UI thread, pinned to the row, every agent alike — and end as the dialog
does (``FleetApp.stop_finished``); what the dialog would have said on its status line is a
warning toast. Cancel and Escape save nothing.

The captain's ``open_stop`` asks whatever this says: tests/test_ui_receiver.py. The switch
itself (flips, Save, Reload, Reset all): tests/test_ui_app_settings.py.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, Checkbox, Switch
from textual.widgets._toast import Toast

from aisquare.cli.ui.app import FleetApp, HelpScreen
from aisquare.cli.ui.sidebar import AppSettingsSelected
from aisquare.cli.ui.stop import ASK_AGAIN_WHERE, DONT_ASK, StopAgentScreen
from aisquare.cli.ui.views.agent import AgentView
from aisquare.core import paths
from aisquare.core.config import load_config, overrides
from aisquare.models import FleetAgent, FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import settings as settings_service
from tests.test_ui_shell import (
    Script,
    StopRecorder,
    drive,
    ended,
    fleet_app,
    open_stop_dialog,
    row_for,
    seed,
    settle,
    status,
    stopper,
)
from tests.test_ui_shell import no_real_tmux as _no_real_tmux
from tests.test_ui_shell import script as _script
from tests.ui_workers import watch_until

#: Re-exported so pytest collects them here (the guard is autouse there, and here).
no_real_tmux = _no_real_tmux
script = _script

CODER = "agt_a_coder-auth"
StopFn = Callable[..., object]


def one_coder(tmp_path: Path, script: Script) -> FleetAgentStatus:
    seed(tmp_path, ("prj_a", "alpha", None))
    live = status("prj_a", "coder-auth", "coder", "working")
    script["prj_a"] = [live]
    return live


def said(app: FleetApp) -> list[tuple[str, str, str]]:
    """Every toast so far: its title, its message and its severity."""
    return [(n.title, str(n.message), n.severity) for n in app._notifications]


def messages(app: FleetApp) -> list[str]:
    return [message for _title, message, _severity in said(app)]


async def open_view(pilot: Pilot[None], app: FleetApp, agent_id: str) -> AgentView:
    await pilot.click(row_for(app, agent_id))
    await pilot.pause()
    await settle(app)
    view = app.current_view()
    assert isinstance(view, AgentView) and view.status.agent.id == agent_id
    return view


async def press_stop(pilot: Pilot[None], app: FleetApp, agent_id: str) -> AgentView:
    """Open the agent's view and press ITS Stop — :meth:`Button.press`, a deliberate press,
    which the click guard lets through as it does a key."""
    view = await open_view(pilot, app, agent_id)
    view.query_one("#agent-stop", Button).press()
    await pilot.pause()
    return view


class RowStopper(StopRecorder):
    """:class:`StopRecorder` answering each row's own ended agent, by the id it is pinned to."""

    def __init__(self, *rows: FleetAgent) -> None:
        super().__init__(rows[0])
        self.rows = {row.id: row for row in rows}

    def __call__(
        self,
        project: ProjectInfo,
        label: str,
        *,
        force: bool = False,
        grace: float = 5.0,
        agent_id: str | None = None,
        actor: str | None = None,
    ) -> fleet_service.StopReceipt:
        assert agent_id is not None, "a stop is pinned to its row"
        self.agent = self.rows[agent_id]
        return super().__call__(
            project, label, force=force, grace=grace, agent_id=agent_id, actor=actor
        )


# --- (1) the tick, and what never saves it ----------------------------------------------


def test_the_dialog_offers_dont_ask_again_unticked_and_cancel_or_escape_saves_nothing(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = one_coder(tmp_path, script)
    recorder = stopper(monkeypatch, live.agent)

    async def go(pilot: Pilot[None]) -> tuple[str, bool, list[str], bool]:
        app = fleet_app(pilot)
        dialog = await open_stop_dialog(pilot, app, CODER)
        tick = dialog.query_one(f"#{DONT_ASK}", Checkbox)
        label, at_open = tick.label.plain, tick.value
        after: list[str] = []
        for close in ("cancel", "escape"):
            if not isinstance(app.screen, StopAgentScreen):
                dialog = await open_stop_dialog(pilot, app, CODER)
            dialog.query_one(f"#{DONT_ASK}", Checkbox).value = True
            if close == "cancel":
                await pilot.click("#stop-cancel")
            else:
                await pilot.press("escape")
            await pilot.pause()
            after.append(type(app.screen).__name__)
        return label, at_open, after, app.confirm_stop

    label, at_open, after, asks = drive(go)
    assert (label, at_open) == ("Don't ask again", False)
    assert "StopAgentScreen" not in after, "both closed the dialog"
    assert asks is True and load_config().ui.confirm_stop is True
    assert "ui" not in overrides(load_config()), "nothing was written"
    assert recorder.calls == []


# --- (2) ticked: saved, told, shown, kept -----------------------------------------------


@pytest.mark.parametrize(("button", "force"), [("#stop-confirm", False), ("#stop-force", True)])
def test_ticked_stop_or_force_saves_dont_ask_again_for_good(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch, button: str, force: bool
) -> None:
    """App settings is opened FIRST, so the page the switch is on was read before the save
    and is kept alive: it must follow the dialog's save, not the file it read."""
    live = one_coder(tmp_path, script)
    recorder = stopper(monkeypatch, live.agent)

    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)
        app.post_message(AppSettingsSelected())
        await settle(app)
        switch = app.query_one("#ui-confirm-stop", Switch)
        seen: dict[str, object] = {"switch before": switch.value}
        dialog = await open_stop_dialog(pilot, app, CODER)
        dialog.query_one(f"#{DONT_ASK}", Checkbox).value = True
        dialog.query_one(button, Button).press()
        await settle(app)
        await pilot.pause()
        seen.update(
            {
                "switch after": switch.value,
                "app asks": app.confirm_stop,
                "dialog open": isinstance(app.screen, StopAgentScreen),
                "said": messages(app),
            }
        )
        return seen

    seen = drive(go)
    assert recorder.calls == [("prj_a", "coder-auth", force, CODER)], "the stop it confirmed"
    assert seen["switch before"] is True
    assert load_config().ui.confirm_stop is False, "saved"
    assert seen["app asks"] is False, "the app holds it at once"
    assert seen["switch after"] is False, "the Settings switch reads off"
    assert seen["dialog open"] is False
    toasts = seen["said"]
    assert isinstance(toasts, list)
    assert f"Stop will not ask again. {ASK_AGAIN_WHERE} turns it back on." in toasts

    async def relaunched(pilot: Pilot[None]) -> tuple[bool, bool, bool]:
        app = fleet_app(pilot)
        app.post_message(AppSettingsSelected())
        await settle(app)
        await press_stop(pilot, app, CODER)
        await settle(app)
        return (
            app.confirm_stop,
            app.query_one("#ui-confirm-stop", Switch).value,
            isinstance(app.screen, StopAgentScreen),
        )

    assert drive(relaunched) == (False, False, False), "it survives a restart of asq"
    assert len(recorder.calls) == 2, "and the next Stop stops at once"


def test_ticked_and_refused_is_still_saved_and_the_dialog_keeps_the_reason(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tick answers the question, not the stop: saved at the press, whatever the stop
    then says. The refusal stays on the dialog's status line, as before, and the tick,
    saved, is greyed rather than saved again."""
    live = one_coder(tmp_path, script)
    recorder = stopper(monkeypatch, live.agent)
    recorder.refuse = fleet_service.FleetError("tmux did not answer, so its row is left live")

    async def go(pilot: Pilot[None]) -> tuple[bool, bool]:
        app = fleet_app(pilot)
        dialog = await open_stop_dialog(pilot, app, CODER)
        dialog.query_one(f"#{DONT_ASK}", Checkbox).value = True
        dialog.query_one("#stop-confirm", Button).press()
        await settle(app)
        await pilot.pause()
        return isinstance(app.screen, StopAgentScreen), dialog.query_one(
            f"#{DONT_ASK}", Checkbox
        ).disabled

    still_open, greyed = drive(go)
    assert still_open and greyed
    assert load_config().ui.confirm_stop is False


def test_an_unreadable_config_is_not_written_over_and_the_stop_goes_on(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A default config saved over a broken one would lose every setting it held for one
    preference: nothing is written, a toast says the dialog will ask again, and the stop
    the user confirmed still runs."""
    live = one_coder(tmp_path, script)
    recorder = stopper(monkeypatch, live.agent)
    broken = "[ui\nnot toml"

    async def go(pilot: Pilot[None]) -> tuple[list[tuple[str, str, str]], bool]:
        app = fleet_app(pilot)
        dialog = await open_stop_dialog(pilot, app, CODER)
        paths.config_path().write_text(broken, encoding="utf-8")  # broken meanwhile
        dialog.query_one(f"#{DONT_ASK}", Checkbox).value = True
        dialog.query_one("#stop-confirm", Button).press()
        await settle(app)
        await pilot.pause()
        return said(app), app.confirm_stop

    toasts, asks = drive(go)
    assert paths.config_path().read_text(encoding="utf-8") == broken
    assert asks is True
    warned = [m for _t, m, severity in toasts if severity == "warning"]
    assert any(
        m.startswith("Don't ask again was not saved, so Stop will ask again: ") for m in warned
    )
    assert recorder.calls == [("prj_a", "coder-auth", False, CODER)]


# --- (3) asking off: one click ------------------------------------------------------------


def test_with_asking_off_stop_and_x_stop_at_once_pinned_to_the_row_manager_too(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's decision (2026-10-30): every agent alike, the manager included — and
    gracefully, the dialog's Stop, never Force. Each ends as the dialog's does: ✓ toasted,
    and the stopped agent's view left for its project's."""
    seed(tmp_path, ("prj_a", "alpha", None))
    manager = status("prj_a", "manager", "manager", "working")
    coder = status("prj_a", "coder-auth", "coder", "working", minute=1)
    script["prj_a"] = [manager, coder]
    recorder = RowStopper(manager.agent, coder.agent)
    monkeypatch.setattr(fleet_service, "stop", recorder)
    settings_service.set_value("ui.confirm_stop", "false")

    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)
        seen: dict[str, object] = {}
        await press_stop(pilot, app, CODER)
        seen["button: dialog"] = isinstance(app.screen, StopAgentScreen)
        wanted = f"✓ stopped coder-auth ({CODER})"
        await watch_until(lambda: wanted in messages(app))
        await settle(app)
        seen["button: left for"] = app.content.current
        await open_view(pilot, app, "agt_a_manager")
        app.sidebar.focus()
        await pilot.press("x")
        await pilot.pause()
        seen["x: dialog"] = isinstance(app.screen, StopAgentScreen)
        await watch_until(lambda: "✓ stopped manager (agt_a_manager)" in messages(app))
        await settle(app)
        seen["said"] = messages(app)
        return seen

    seen = drive(go)
    assert recorder.calls == [
        ("prj_a", "coder-auth", False, CODER),
        ("prj_a", "manager", False, "agt_a_manager"),
    ]
    assert seen["button: dialog"] is False and seen["x: dialog"] is False
    assert seen["button: left for"] == "project-prj_a"
    said_ = seen["said"]
    assert isinstance(said_, list)
    assert "stopping coder-auth …" in said_ and "stopping manager …" in said_


def test_a_stop_without_asking_runs_off_the_ui_thread_and_a_second_press_does_nothing(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``stop`` blocks (/exit, the grace, the kill): the service here holds until the test
    lets it go, and the presses made meanwhile — the button again, then ``x`` — ask it
    nothing. Run on the UI thread, the press could not return until the service gave up on
    its own, which ``timed_out`` records: a red, rather than a hang."""
    live = one_coder(tmp_path, script)
    settings_service.set_value("ui.confirm_stop", "false")
    entered = threading.Event()
    release = threading.Event()
    calls: list[str | None] = []
    timed_out: list[bool] = []

    def blocking_stop(
        project: ProjectInfo,
        label: str,
        *,
        force: bool = False,
        grace: float = 5.0,
        agent_id: str | None = None,
        actor: str | None = None,
    ) -> fleet_service.StopReceipt:
        calls.append(agent_id)
        entered.set()
        if not release.wait(10):
            timed_out.append(True)
        return fleet_service.StopReceipt(ended(live.agent), [])

    monkeypatch.setattr(fleet_service, "stop", blocking_stop)

    async def go(pilot: Pilot[None]) -> tuple[bool, int, bool]:
        app = fleet_app(pilot)
        view = await press_stop(pilot, app, CODER)
        started = entered.wait(5)
        view.query_one("#agent-stop", Button).press()  # again, while it runs
        await pilot.pause()
        app.sidebar.focus()
        await pilot.press("x")  # and the key
        await pilot.pause()
        meanwhile = len(calls)
        dialog = isinstance(app.screen, StopAgentScreen)
        release.set()
        await watch_until(lambda: f"✓ stopped coder-auth ({CODER})" in messages(app))
        await settle(app)
        return started, meanwhile, dialog

    started, meanwhile, dialog = drive(go)
    assert timed_out == [], "the stop ran on the UI thread: the app could not release it"
    assert started and meanwhile == 1 and not dialog
    assert calls == [CODER], "one stop, pinned to the row"


def _stopped_without_asking(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch, stop: StopFn
) -> tuple[list[tuple[str, str, str]], list[str], str]:
    """Press Stop once with asking off, against ``stop``: every toast, as recorded and as
    rendered, and the view on screen once the answer has been handled."""
    one_coder(tmp_path, script)
    settings_service.set_value("ui.confirm_stop", "false")
    monkeypatch.setattr(fleet_service, "stop", stop)

    async def go(pilot: Pilot[None]) -> tuple[list[tuple[str, str, str]], list[str], str]:
        app = fleet_app(pilot)
        await press_stop(pilot, app, CODER)
        await watch_until(lambda: any(s != "information" for _t, _m, s in said(app)))
        await settle(app)
        rendered = [toast.render().plain for toast in app.screen.query(Toast)]
        return said(app), rendered, type(app.current_view()).__name__

    return drive(go, notifications=True)


def test_a_refusal_without_asking_is_a_warning_in_the_services_own_words(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where the dialog kept the reason on its status line, a toast does — as data: the
    ``[red]`` in it is a real Rich style, and must reach the screen as typed."""
    reason = (
        "could not stop 'coder-auth' ([red] pane 3 refused, tmux did not answer) — its pane "
        "is still alive, so its row is left live"
    )

    def refusing(project: ProjectInfo, label: str, **kw: object) -> fleet_service.StopReceipt:
        raise fleet_service.FleetError(reason)

    toasts, rendered, view = _stopped_without_asking(tmp_path, script, monkeypatch, refusing)
    assert ("coder-auth not stopped", reason, "warning") in toasts
    assert any(reason in line for line in rendered), rendered
    assert not any(m.startswith("✓ stopped") for _t, m, _s in toasts)
    assert view == "AgentView", "nothing stopped: the agent's view stays"


def test_a_stop_without_a_receipt_is_a_warning_and_no_stop(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_receipt(project: ProjectInfo, label: str, **kw: object) -> None:
        return None

    toasts, _rendered, view = _stopped_without_asking(tmp_path, script, monkeypatch, no_receipt)
    assert (
        "coder-auth not stopped",
        "the stop answered without a receipt (NoneType)",
        "warning",
    ) in toasts
    assert view == "AgentView"


def test_claims_a_stop_without_asking_could_not_release_are_warned_of(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fleet stop`` prints this and exits 1; the dialog warns of it, and so does this."""
    live = status("prj_a", "coder-auth", "coder", "working")

    def half(project: ProjectInfo, label: str, **kw: object) -> fleet_service.StopReceipt:
        return fleet_service.StopReceipt(ended(live.agent), [], "the store was [locked]")

    toasts, _rendered, _view = _stopped_without_asking(tmp_path, script, monkeypatch, half)
    assert ("", "claims: the store was [locked]", "warning") in toasts
    assert any(m == f"✓ stopped coder-auth ({CODER})" for _t, m, _s in toasts)


def test_the_help_says_whether_x_asks_first() -> None:
    def x_line(confirm_stop: bool) -> str:
        text = HelpScreen("f12", confirm_stop=confirm_stop).keys_text().plain
        return next(line for line in text.splitlines() if "stop the selected agent" in line)

    assert x_line(True).endswith("(asks first)")
    assert "at once" in x_line(False) and "asks first" not in x_line(False)
