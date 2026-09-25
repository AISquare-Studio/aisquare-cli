"""The captain's corner of the fleet UI: the rank insignia, the captain view, its quick action (T4).

Acceptance (card T4): the glyph state follows the row; a click opens the view;
the quick action types into the pane (a recorder on ``fleet.tell``); the
thinking indicator follows the flag; the divider, groups and existing bindings
are untouched (their own suites stay green). Driven with the UI suite's
harness: the real store in the isolated home, ``list_agents`` scripted, every
tmux call held to a private socket.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, Input, Static

from aisquare.cli.ui.sidebar import AgentRow, CaptainButton
from aisquare.cli.ui.spawn import SpawnDialog
from aisquare.cli.ui.views.agent import AgentView
from aisquare.cli.ui.views.captain import WHAT_IS_UP, CaptainView
from aisquare.models import FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services.captain import brain
from aisquare.services.captain import state as captain_state
from tests import test_ui_shell as ui_suite
from tests.test_ui_shell import Script, drive, fleet_app, row_for, seed, shown, status

# The UI suite's fixtures, bound here so pytest finds them for this module's tests
# (``no_real_tmux`` is autouse there: every tmux call held to a private socket).
no_real_tmux = ui_suite.no_real_tmux
script = ui_suite.script


def _captain(state: str = "waiting") -> FleetAgentStatus:
    return status(captain_state.home_project().id, "captain", "captain", state)


def _insignia(pilot: Pilot[None]) -> CaptainButton:
    return fleet_app(pilot).query_one("#captain-button", CaptainButton)


# --- the insignia ---------------------------------------------------------------------------


def test_the_insignia_sits_in_the_fleet_header_between_fleet_and_plus(
    tmp_path: Path, script: Script
) -> None:
    async def body(pilot: Pilot[None]) -> None:
        header = fleet_app(pilot).query_one("#fleet-header")
        assert [child.id for child in header.children] == [
            "fleet-title",
            "captain-button",
            "add-project",
        ]
        assert "★" in shown(_insignia(pilot))

    drive(body)


@pytest.mark.parametrize(
    ("state", "lit"),
    [("waiting", True), ("working", True), ("attention", True), ("exited", False), (None, False)],
)
def test_the_insignia_is_lit_while_the_captain_row_is_live_and_dim_otherwise(
    tmp_path: Path, script: Script, state: str | None, lit: bool
) -> None:
    """Plan section 4: lit in the accent while the captain row is live, dim otherwise."""
    seed(tmp_path, ("prj_aaa", "alpha", "amber-otter"))
    if state is not None:
        script[captain_state.home_project().id] = [_captain(state)]

    async def body(pilot: Pilot[None]) -> None:
        fleet_app(pilot).refresh_data()
        await pilot.pause()
        assert _insignia(pilot).has_class("live") is lit

    drive(body)


def test_the_insignia_follows_the_row_from_frame_to_frame(tmp_path: Path, script: Script) -> None:
    home = captain_state.home_project()

    async def body(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        app.refresh_data()
        await pilot.pause()
        assert not _insignia(pilot).has_class("live")
        script[home.id] = [_captain("working")]
        app.refresh_data()
        await pilot.pause()
        assert _insignia(pilot).has_class("live")
        script[home.id] = []
        app.refresh_data()
        await pilot.pause()
        assert not _insignia(pilot).has_class("live")

    drive(body)


def test_the_lit_insignia_opens_the_captain_view_over_its_pane(
    tmp_path: Path, script: Script
) -> None:
    captain = _captain()
    script[captain_state.home_project().id] = [captain]

    async def body(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        app.refresh_data()
        await pilot.pause()
        await pilot.click("#captain-button")
        await pilot.pause()
        view = app.current_view()
        assert isinstance(view, CaptainView)
        assert view.status.agent.id == captain.agent.id
        assert view.pane.pane_id == captain.agent.pane_id
        assert app.sidebar.selected_key == f"agent:{captain.agent.id}"

    drive(body)


def test_the_dim_insignia_opens_the_spawn_dialog_preset_to_the_captain(
    tmp_path: Path, script: Script
) -> None:
    async def body(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        app.refresh_data()
        await pilot.pause()
        await pilot.click("#captain-button")
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, SpawnDialog)
        assert dialog.project.id == captain_state.home_project().id
        assert dialog._role == "captain"
        assert dialog.query_one("#spawn-label", Input).value == "captain"
        assert dialog.query_one("#spawn-label", Input).disabled, "one per home: the label is fixed"
        assert dialog.query_one("#spawn-args", Input).disabled, "its arguments are its own"
        assert "Start the captain" in shown(dialog.query_one("#spawn-header", Static))

    drive(body)


def test_a_dim_insignia_over_an_exited_captain_offers_a_start_not_its_dead_pane(
    tmp_path: Path, script: Script
) -> None:
    """A 💤 row still lists for a while (``remain-on-exit``); the star is dim, and a click
    does what the dim star says — the Spawn dialog, whose start replaces the dead row."""
    script[captain_state.home_project().id] = [_captain("exited")]

    async def body(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        app.refresh_data()
        await pilot.pause()
        await pilot.click("#captain-button")
        await pilot.pause()
        assert isinstance(app.screen, SpawnDialog)

    drive(body)


@dataclass
class Started:
    calls: list[dict[str, Any]] = field(default_factory=list)
    order: list[str] = field(default_factory=list)


def test_the_captain_dialog_starts_the_captain_through_its_own_launch(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fleet.spawn`` refuses a captain without its brain folder and its one server (T2);
    the dialog's Spawn is ``brain.start`` with what the form chose."""
    started = Started()
    home = captain_state.home_project()

    def start(prompt: str | None = None, **choices: Any) -> fleet_service.SpawnReceipt:
        started.order.append("start")
        started.calls.append({"prompt": prompt, **choices})
        row = _captain().agent
        return fleet_service.SpawnReceipt(agent=row, asked_label="captain", tmux_session="asq-h")

    def find() -> None:
        started.order.append("find")  # a dead or vanished captain is ended before the start

    monkeypatch.setattr(brain, "start", start)
    monkeypatch.setattr(brain, "find", find)
    monkeypatch.setattr(
        fleet_service, "spawn", lambda *a, **k: pytest.fail("the captain is not fleet-spawned")
    )

    async def body(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        app.refresh_data()
        await pilot.pause()
        await pilot.click("#captain-button")
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, SpawnDialog)
        await pilot.click("#spawn-submit")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    drive(body)
    assert len(started.calls) == 1
    call = started.calls[0]
    assert call["prompt"] is None and call["persona"] == "captain"
    assert "agent_args" not in call, "the captain's arguments are its own, never the form's"
    assert started.order == ["find", "start"]
    assert home.id == captain_state.home_project().id


# --- the captain view -------------------------------------------------------------------------


def test_selecting_the_captain_row_opens_the_captain_view(tmp_path: Path, script: Script) -> None:
    seed(tmp_path, ("prj_aaa", "alpha", "amber-otter"))
    captain = _captain()
    script[captain_state.home_project().id] = [captain]
    script["prj_aaa"] = [status("prj_aaa", "coder-1", "coder", "working")]

    async def body(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        app.refresh_data()
        await pilot.pause()
        row_for(app, captain.agent.id).activate()
        await pilot.pause()
        assert isinstance(app.current_view(), CaptainView)
        row_for(app, "agt_aaa_coder-1").activate()
        await pilot.pause()
        view = app.current_view()
        assert isinstance(view, AgentView) and not isinstance(view, CaptainView)

    drive(body)


@dataclass
class Told:
    calls: list[tuple[str, str, str]] = field(default_factory=list)


def _record_tell(monkeypatch: pytest.MonkeyPatch) -> Told:
    told = Told()

    def tell(project: ProjectInfo, label: str, text: str, **_: Any) -> fleet_service.TellResult:
        told.calls.append((project.id, label, text))
        return fleet_service.TellResult(True, "typed into the pane")

    monkeypatch.setattr(fleet_service, "tell", tell)
    return told


def test_the_quick_action_tells_the_captain_what_is_up(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    told = _record_tell(monkeypatch)
    home = captain_state.home_project()
    captain = _captain("waiting")
    script[home.id] = [captain]

    async def body(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        app.refresh_data()
        await pilot.pause()
        await pilot.click("#captain-button")
        await pilot.pause()
        await pilot.click("#captain-whats-up")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    drive(body)
    assert told.calls == [(home.id, "captain", WHAT_IS_UP)]


def test_the_quick_action_waits_for_a_captain_at_its_prompt(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Typed only into a WAITING captain: ``tell`` files anything else as a board note,
    and the captain has no shell to read one (T2's delivery rule)."""
    told = _record_tell(monkeypatch)
    home = captain_state.home_project()
    script[home.id] = [_captain("working")]

    async def body(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        app.refresh_data()
        await pilot.pause()
        await pilot.click("#captain-button")
        await pilot.pause()
        button = app.query_one("#captain-whats-up", Button)
        assert button.disabled
        script[home.id] = [_captain("waiting")]
        app.refresh_data()
        await pilot.pause()
        assert not button.disabled

    drive(body)
    assert told.calls == []


def _thinking(pilot: Pilot[None]) -> str:
    return shown(fleet_app(pilot).query_one("#captain-thinking", Static))


def test_the_thinking_indicator_follows_the_busy_flag(tmp_path: Path, script: Script) -> None:
    """T1's ``thinking on`` sets the flag in state.json, from another process — the view
    reads it on its own tick, not only when the row's state changes."""
    script[captain_state.home_project().id] = [_captain("waiting")]

    async def body(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        app.refresh_data()
        await pilot.pause()
        await pilot.click("#captain-button")
        await pilot.pause()
        view = app.current_view()
        assert isinstance(view, CaptainView)
        assert "idle" in _thinking(pilot)
        captain_state.set_busy(True)
        view.paint_thinking()
        await pilot.pause()
        assert "thinking" in _thinking(pilot)
        captain_state.set_busy(False)
        view.paint_thinking()
        await pilot.pause()
        assert "idle" in _thinking(pilot)

    drive(body)


def test_the_thinking_indicator_reads_a_working_pane_as_thinking(
    tmp_path: Path, script: Script
) -> None:
    home = captain_state.home_project()
    script[home.id] = [_captain("waiting")]

    async def body(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        app.refresh_data()
        await pilot.pause()
        await pilot.click("#captain-button")
        await pilot.pause()
        assert "idle" in _thinking(pilot)
        script[home.id] = [_captain("working")]
        app.refresh_data()
        await pilot.pause()
        assert "thinking" in _thinking(pilot)

    drive(body)


def test_the_view_ticks_the_flag_on_its_own(tmp_path: Path, script: Script) -> None:
    """The row's state does not change while the flag does, and the shell feeds a view
    only on a changed status — so the view keeps its own interval."""
    script[captain_state.home_project().id] = [_captain("waiting")]

    async def body(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        app.refresh_data()
        await pilot.pause()
        await pilot.click("#captain-button")
        await pilot.pause()
        view = app.current_view()
        assert isinstance(view, CaptainView)
        assert view.thinking_timer is not None

    drive(body)


def test_the_captain_row_is_still_an_ordinary_agent_row(tmp_path: Path, script: Script) -> None:
    """T2's section is untouched: the insignia is an extra door, not a second row."""
    captain = _captain()
    script[captain_state.home_project().id] = [captain]

    async def body(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        app.refresh_data()
        await pilot.pause()
        rows = list(app.query_one("#captain-section").query(AgentRow))
        assert [row.status.agent.id for row in rows] == [captain.agent.id]

    drive(body)


# --- the voice controls (T3's page: services.captain.voice and speaker) -----------------------


def _open_captain(pilot: Pilot[None]) -> CaptainView:
    view = fleet_app(pilot).current_view()
    assert isinstance(view, CaptainView)
    return view


async def _captain_view(pilot: Pilot[None]) -> CaptainView:
    app = fleet_app(pilot)
    app.refresh_data()
    await pilot.pause()
    await pilot.click("#captain-button")
    await pilot.pause()
    return _open_captain(pilot)


async def _clicked(pilot: Pilot[None], selector: str) -> None:
    """A click whose write runs in a worker: wait for it, then for the repaint it asks for.

    Then past the button's press effect: Textual ignores a click on a Button still
    showing it (``-active``, 0.2 s), as a person's double click flips a switch once.
    """
    await pilot.click(selector)
    await pilot.pause()
    await fleet_app(pilot).workers.wait_for_complete()
    await pilot.pause(0.3)


def _label(pilot: Pilot[None], selector: str) -> str:
    return str(fleet_app(pilot).query_one(selector, Button).label)


def test_the_speaker_toggle_flips_the_one_switch_both_ways(tmp_path: Path, script: Script) -> None:
    """T3's ``captain_speaker`` in state.json: the page's toggle, the CLI's --speaker, and this."""
    from aisquare.services.captain import speaker

    script[captain_state.home_project().id] = [_captain()]

    async def body(pilot: Pilot[None]) -> None:
        await _captain_view(pilot)
        assert speaker.speaker_on() and _label(pilot, "#captain-speaker") == "Speaker: on"
        await _clicked(pilot, "#captain-speaker")
        assert not speaker.speaker_on()
        assert _label(pilot, "#captain-speaker") == "Speaker: off"
        await _clicked(pilot, "#captain-speaker")
        assert speaker.speaker_on() and _label(pilot, "#captain-speaker") == "Speaker: on"

    drive(body)


def test_the_mode_toggle_writes_the_one_mode_key(tmp_path: Path, script: Script) -> None:
    """13178/13179: ``captain_voice_mode`` is the mode's single home; unset reads as focus."""
    from aisquare.services.captain import voice

    script[captain_state.home_project().id] = [_captain()]

    async def body(pilot: Pilot[None]) -> None:
        await _captain_view(pilot)
        assert voice.voice_mode() is None and _label(pilot, "#captain-mode") == "Mode: focus"
        await _clicked(pilot, "#captain-mode")
        assert voice.voice_mode() == "listen" and _label(pilot, "#captain-mode") == "Mode: listen"
        await _clicked(pilot, "#captain-mode")
        assert voice.voice_mode() == "focus" and _label(pilot, "#captain-mode") == "Mode: focus"

    drive(body)


def test_the_view_follows_what_the_page_changed(tmp_path: Path, script: Script) -> None:
    """The page writes the same keys from another process; the view's tick reads them."""
    from aisquare.services.captain import speaker, voice

    script[captain_state.home_project().id] = [_captain()]

    async def body(pilot: Pilot[None]) -> None:
        view = await _captain_view(pilot)
        voice.set_voice_mode("listen")
        speaker.set_speaker(False)
        view.paint_thinking()  # the view's one tick paints the whole bar
        await pilot.pause()
        assert _label(pilot, "#captain-mode") == "Mode: listen"
        assert _label(pilot, "#captain-speaker") == "Speaker: off"

    drive(body)


def _mic_text(pilot: Pilot[None]) -> str:
    from aisquare.cli.ui.views.captain import MicScreen

    screen = fleet_app(pilot).screen
    assert isinstance(screen, MicScreen)
    return "\n".join(shown(line) for line in screen.query(Static))


async def _press_mic(pilot: Pilot[None]) -> str:
    await _captain_view(pilot)
    await pilot.click("#captain-mic")
    await pilot.pause()
    await fleet_app(pilot).workers.wait_for_complete()
    await pilot.pause()
    return _mic_text(pilot)


def test_the_mic_prints_the_url_the_qr_and_the_start_command_with_no_page_running(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """13178 Q2: print only — with none running, the URL it would serve, the QR, the command."""
    from aisquare.cli.ui.views import captain as captain_view
    from aisquare.services.captain import voice
    from aisquare.services.mcp_server import serve_token

    script[captain_state.home_project().id] = [_captain()]
    monkeypatch.setattr(captain_view, "page_serving", lambda port: False)
    monkeypatch.setattr(voice, "qr_lines", lambda url: ["█▀▀█ qr", "█▄▄█ qr"])

    async def body(pilot: Pilot[None]) -> str:
        return await _press_mic(pilot)

    text = drive(body)
    assert voice.voice_url(voice.DEFAULT_PORT, serve_token()) in text
    assert "█▀▀█ qr" in text and "█▄▄█ qr" in text
    assert "aisquare captain voice" in text
    assert voice.adb_reverse(voice.DEFAULT_PORT) in text
    assert "not running" in text


def test_the_mic_says_a_page_that_is_serving(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.cli.ui.views import captain as captain_view
    from aisquare.services.captain import voice

    script[captain_state.home_project().id] = [_captain()]
    monkeypatch.setattr(captain_view, "page_serving", lambda port: True)
    monkeypatch.setattr(voice, "qr_lines", lambda url: None)

    async def body(pilot: Pilot[None]) -> str:
        return await _press_mic(pilot)

    text = drive(body)
    assert "serving" in text and "not running" not in text
    assert "segno" in text, "no QR without segno — and it says why"


def test_page_serving_is_a_real_connect_on_loopback() -> None:
    """The probe answers False for a port nothing listens on, True for one that does."""
    import socket

    from aisquare.cli.ui.views.captain import page_serving

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert page_serving(port)
    assert not page_serving(port)
