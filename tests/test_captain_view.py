"""The captain's corner of the fleet UI: the rank insignia, the captain view, its quick action (T4).

Acceptance (card T4): the glyph state follows the row; a click opens the view;
the quick action types into the pane (a recorder on ``fleet.tell``); the
thinking indicator follows the flag; the divider, groups and existing bindings
are untouched (their own suites stay green). Driven with the UI suite's
harness: the real store in the isolated home, ``list_agents`` scripted, every
tmux call held to a private socket.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

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
from tests import test_captain_say as say_suite
from tests import test_ui_shell as ui_suite
from tests.captain_screens import REAL_TRUST
from tests.test_captain_say import Captain, Clock
from tests.test_captain_sidebar import agent_opened, quiet, until
from tests.test_ui_shell import Script, fleet_app, row_for, seed, shown, status

# The UI suite's fixtures, bound here so pytest finds them for this module's tests
# (``no_real_tmux`` is autouse there: every tmux call held to a private socket).
no_real_tmux = ui_suite.no_real_tmux
script = ui_suite.script

T = TypeVar("T")


def drive(body: Callable[[Pilot[None]], Awaitable[T]], **options: Any) -> T:
    """The suite's ``drive``, with the app's workers answered before the body returns
    (``quiet``): a Doctor scope change or a tell's worker must not land in teardown."""

    async def settled(pilot: Pilot[None]) -> T:
        result = await body(pilot)
        await quiet(pilot)
        return result

    return ui_suite.drive(settled, **options)


async def _open_view(pilot: Pilot[None]) -> CaptainView:
    """Click the lit star and wait for the selection's last effect, not for a pause
    (``tests.test_captain_sidebar.until`` says why: Windows returned mid-handler)."""
    app = fleet_app(pilot)
    home = captain_state.home_project()
    rows = app.snapshot.agents.get(home.id, []) if app.snapshot else []
    assert rows, "a live captain row to open"
    await pilot.click("#captain-button")
    await until(pilot, agent_opened(pilot, rows[0].agent.id), what="the captain view open")
    view = app.current_view()
    assert isinstance(view, CaptainView)
    return view


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
        await _open_view(pilot)
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
        await until(pilot, agent_opened(pilot, captain.agent.id), what="the captain selected")
        assert isinstance(app.current_view(), CaptainView)
        row_for(app, "agt_aaa_coder-1").activate()
        await until(pilot, agent_opened(pilot, "agt_aaa_coder-1"), what="the coder selected")
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


captain_door = say_suite.captain
"""T2's say/send stand-in (the captain's row, pane and tmux), as the fixture ``captain``."""


async def _whats_up(pilot: Pilot[None]) -> list[str]:
    """Click What's up, let its worker answer, and return what the shell said."""
    app = fleet_app(pilot)
    app.refresh_data()
    await pilot.pause()
    await _open_view(pilot)
    await pilot.click("#captain-whats-up")
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()
    return [str(note.message) for note in app._notifications]


def test_the_quick_action_types_through_the_guarded_door_never_fleet_tell(
    tmp_path: Path, script: Script, captain_door: tuple[Captain, Clock]
) -> None:
    """13325 B1: T2's brain.send, which reads the pane first — a drawn input box, typed."""
    fake, _ = captain_door
    fake.present()  # type: ignore[attr-defined]
    script[captain_state.home_project().id] = [_captain("waiting")]
    said = drive(_whats_up)
    assert fake.typed == [("paste", WHAT_IS_UP), ("keys", "Enter")]
    assert fake.told == [], "never fleet.tell"
    assert any(f"asked the captain: {WHAT_IS_UP}" in line for line in said), said


def test_the_quick_action_types_nothing_into_a_fresh_captains_trust_dialog(
    tmp_path: Path, script: Script, captain_door: tuple[Captain, Clock]
) -> None:
    """The trap B1 names: a fresh captain at Claude Code's trust dialog reads waiting, so the
    button is on — and an Enter there picks "No, exit". The real capture, refused by name."""
    fake, _ = captain_door
    fake.present()  # type: ignore[attr-defined]
    fake.screen = list(REAL_TRUST)
    script[captain_state.home_project().id] = [_captain("waiting")]
    said = drive(_whats_up)
    assert fake.typed == [] and fake.told == []
    assert any("nothing typed" in line and "trust its folder" in line for line in said), said
    assert any("choose Yes, I trust this folder (once)" in line for line in said), said


def test_the_quick_action_waits_for_a_captain_at_its_prompt(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Offered only to a WAITING captain; a working one is greyed, never queued."""
    told = _record_tell(monkeypatch)
    home = captain_state.home_project()
    script[home.id] = [_captain("working")]

    async def body(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        app.refresh_data()
        await pilot.pause()
        await _open_view(pilot)
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
        await _open_view(pilot)
        view = app.current_view()
        assert isinstance(view, CaptainView)
        assert "idle" in _thinking(pilot)
        captain_state.set_busy(True)
        await _ticked(pilot, view)
        assert "thinking" in _thinking(pilot)
        captain_state.set_busy(False)
        await _ticked(pilot, view)
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
        await _open_view(pilot)
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
        await _open_view(pilot)
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


async def _captain_view(pilot: Pilot[None]) -> CaptainView:
    app = fleet_app(pilot)
    app.refresh_data()
    await pilot.pause()
    return await _open_view(pilot)


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
        await _ticked(pilot, view)  # the view's one tick paints the whole bar
        assert _label(pilot, "#captain-mode") == "Mode: listen"
        assert _label(pilot, "#captain-speaker") == "Speaker: off"

    drive(body)


async def _ticked(pilot: Pilot[None], view: CaptainView) -> None:
    """One tick of the bar: its read runs on a worker (S1), then the paint."""
    view.tick()
    await pilot.pause()
    await fleet_app(pilot).workers.wait_for_complete()
    await pilot.pause()


def test_the_bar_reads_state_json_off_the_ui_thread(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """state.json's read retries under Windows contention for up to ~0.9 s: never on the
    UI loop, once a second (T4 gate S1, 13325)."""
    import threading

    from aisquare.services.captain import speaker, voice

    script[captain_state.home_project().id] = [_captain()]
    threads: list[str] = []

    def spied(read: Callable[[], Any]) -> Callable[[], Any]:
        def run() -> Any:
            threads.append(threading.current_thread().name)
            return read()

        return run

    monkeypatch.setattr(captain_state, "busy_since", spied(captain_state.busy_since))
    monkeypatch.setattr(voice, "voice_mode", spied(voice.voice_mode))
    monkeypatch.setattr(speaker, "speaker_on", spied(speaker.speaker_on))

    async def body(pilot: Pilot[None]) -> None:
        view = await _captain_view(pilot)
        await _ticked(pilot, view)

    drive(body)
    main = threading.main_thread().name
    assert threads, "the bar read state.json"
    assert main not in threads, f"read on the UI thread: {threads}"


def test_an_unreadable_state_file_is_said_on_the_bar_never_shown_as_the_defaults(
    tmp_path: Path,
    script: Script,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T4 gate S2 (13325): focus and speaker-on are defaults, not facts, when the file cannot
    be read — the bar says so, and says it again when the file reads."""
    from aisquare.services.captain import speaker, voice

    script[captain_state.home_project().id] = [_captain()]
    real = voice.voice_mode, speaker.speaker_on, captain_state.busy_since

    def unreadable() -> Any:
        raise OSError("state.json: permission denied")

    async def body(pilot: Pilot[None]) -> list[tuple[str, str, str]]:
        view = await _captain_view(pilot)
        seen = []
        for what in (unreadable, None):
            monkeypatch.setattr(voice, "voice_mode", what or real[0])
            monkeypatch.setattr(speaker, "speaker_on", what or real[1])
            monkeypatch.setattr(captain_state, "busy_since", what or real[2])
            await _ticked(pilot, view)
            note = fleet_app(pilot).query_one("#captain-state", Static)
            seen.append(
                (
                    _label(pilot, "#captain-mode"),
                    _label(pilot, "#captain-speaker"),
                    shown(note) if note.display else "",
                )
            )
        return seen

    broken, mended = drive(body)
    assert broken[:2] == ("Mode: ?", "Speaker: ?") and "state unreadable" in broken[2]
    assert mended == ("Mode: focus", "Speaker: on", "")
    assert any("permission denied" in record.getMessage() for record in caplog.records)


def test_a_switch_over_an_unreadable_state_writes_nothing_and_says_why(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flipping a switch whose state is unknown would write a guess."""
    from aisquare.services.captain import speaker, voice

    script[captain_state.home_project().id] = [_captain()]
    written: list[object] = []

    def unreadable() -> Any:
        raise OSError("state.json: permission denied")

    async def body(pilot: Pilot[None]) -> list[str]:
        view = await _captain_view(pilot)
        monkeypatch.setattr(voice, "voice_mode", unreadable)
        monkeypatch.setattr(speaker, "speaker_on", unreadable)
        monkeypatch.setattr(voice, "set_voice_mode", written.append)
        monkeypatch.setattr(speaker, "set_speaker", written.append)
        await _ticked(pilot, view)
        await _clicked(pilot, "#captain-mode")
        await _clicked(pilot, "#captain-speaker")
        return [str(n.message) for n in fleet_app(pilot)._notifications]

    said = drive(body)
    assert written == []
    assert sum("state unreadable" in line for line in said) == 2, said


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


def _http(port_body: bytes) -> tuple[Any, int]:
    """A loopback HTTP server answering ``/`` with ``port_body``, on a free port."""
    import http.server
    import threading

    class Page(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(port_body)))
            self.end_headers()
            self.wfile.write(port_body)

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def test_page_serving_is_the_voice_page_itself_not_any_listener() -> None:
    """T4 gate S3 (13325): a connect took ANY listener on the port for the page. Now the
    page's own bytes, from ``GET /`` — what T3's server answers, token or not."""
    import socket

    from aisquare.cli.ui.views.captain import page_serving
    from aisquare.services.captain import voice

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert not page_serving(port), "a listener that says nothing is not the page"
    assert not page_serving(port), "nothing listens"
    for body, serving in ((voice.page_bytes(), True), (b"<title>captain</title>", False)):
        server, port = _http(body)
        try:
            assert page_serving(port) is serving, body[:40]
        finally:
            server.shutdown()
            server.server_close()
