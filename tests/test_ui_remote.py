"""The ``m`` modal: opens from the sidebar, toggles Remote, shows the ngrok hint, survives restarts.

Driven headless with ``App.run_test`` at 140x40 as ``test_ui_shell.py`` drives the
shell. The server is the REAL ``services.remote_server`` on a free port (its
process-wide runtime is reset per test, as its own tests do); the tunnel
factory is scripted per test — ngrok is absent on the build machine, and
the acceptance says that case must be a sentence in the modal, not a crash.
Every assertion reads what a widget SHOWS.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Awaitable, Callable, Iterator, Sequence
from typing import TypeVar

import pytest
from textual.pilot import Pilot
from textual.widgets import Select, Static, Switch

from aisquare.cli.ui.app import FleetApp, HelpScreen
from aisquare.cli.ui.remote_control import READ_ONLY_REASON, RemoteController
from aisquare.cli.ui.views.remote import RemotePanel, qr_text
from aisquare.core import paths
from aisquare.core import tmux as tmux_core
from aisquare.core.tmux import Completed
from aisquare.models import FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import remote_server
from aisquare.services.ngrok_tunnel import INSTALL_HINT, NgrokTunnel, build_public_url
from tests.test_remote_control import FakeTunnel, fake_tunnel_factory

T = TypeVar("T")
SIZE = (140, 40)
PUBLIC = "https://abcd-12.ngrok-free.app"


@pytest.fixture(autouse=True)
def no_real_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(argv: Sequence[str], stdin: bytes | None) -> Completed:
        return Completed(1, "", "no server running (a UI test addresses no real fleet)\n")

    monkeypatch.setattr(tmux_core, "_tmux", refuse)


@pytest.fixture(autouse=True)
def fresh_remote_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The server's runtime is loaded once per process; each test's home is new."""
    monkeypatch.setattr(remote_server, "_runtime", None)
    yield
    remote_server.stop()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(autouse=True)
def no_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    def none(project: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
        return []

    monkeypatch.setattr(fleet_service, "list_agents", none)


def drive(
    fn: Callable[[Pilot[None]], Awaitable[T]],
    *,
    tunnel: Callable[[int], NgrokTunnel],
) -> T:
    """Run ``fn`` against a mounted ``FleetApp`` whose Remote uses the stub server + ``tunnel``."""

    async def run() -> T:
        controller = RemoteController(
            server=remote_server, tunnel_factory=tunnel, url_timeout=2, port=free_port()
        )
        app = FleetApp(refresh_seconds=3600, doctor=lambda: [], remote=controller)
        async with app.run_test(size=SIZE, notifications=True) as pilot:
            await pilot.pause()
            return await fn(pilot)

    return asyncio.run(run())


def shown(widget: Static) -> str:
    visual = widget.visual
    plain = getattr(visual, "plain", None)
    assert isinstance(plain, str), f"{widget!r} renders a {type(visual).__name__}, not text"
    return plain


def panel(pilot: Pilot[None]) -> RemotePanel:
    screen = pilot.app.screen
    assert isinstance(screen, RemotePanel), f"the top screen is a {type(screen).__name__}"
    return screen


async def open_panel(pilot: Pilot[None]) -> RemotePanel:
    await pilot.press("m")
    await pilot.pause()
    return panel(pilot)


def sessions() -> list[dict[str, str]]:
    listed = remote_server.status()["sessions"]
    assert isinstance(listed, list)
    return listed


def missing_ngrok(port: int) -> NgrokTunnel:
    return FakeTunnel(port, url=None, failure=INSTALL_HINT)


# --- open, toggle, close -------------------------------------------------------------------------


def test_m_opens_the_remote_panel_and_the_switch_turns_remote_on_and_off() -> None:
    async def go(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        assert not isinstance(app.screen, RemotePanel)
        modal = await open_panel(pilot)
        assert shown(modal.query_one("#remote-state", Static)) == "off"
        assert shown(modal.query_one("#remote-link", Static)) == "turn Remote on for a link"
        assert shown(modal.query_one("#remote-password", Static)) == "—"

        switch = modal.query_one("#remote-on", Switch)
        switch.toggle()
        await pilot.pause()
        for _ in range(20):  # the URL arrives from the tunnel's thread; the modal ticks
            if app.remote.public_url is not None:
                break
            await asyncio.sleep(0.1)
        modal.repaint()
        await pilot.pause()
        assert app.remote.running
        assert remote_server.status()["running"] is True
        expected = build_public_url(PUBLIC, app.remote.info.token)  # type: ignore[union-attr]
        assert shown(modal.query_one("#remote-link", Static)) == expected
        assert shown(modal.query_one("#remote-state", Static)).startswith("on")
        password = shown(modal.query_one("#remote-password", Static))
        assert password == remote_server.runtime().password and len(password) == 8
        qr = shown(modal.query_one("#remote-qr", Static))
        assert qr == qr_text(expected) and 15 <= len(qr.splitlines()) <= 22
        assert shown(modal.query_one("#remote-write-hint", Static)) == READ_ONLY_REASON
        assert modal.query_one("#remote-allow-write", Switch).value is False

        switch.toggle()
        await pilot.pause()
        assert not app.remote.running
        assert remote_server.status()["running"] is False
        assert shown(modal.query_one("#remote-state", Static)) == "off"
        assert shown(modal.query_one("#remote-qr", Static)) == ""

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, RemotePanel)

    drive(go, tunnel=fake_tunnel_factory(url=PUBLIC))


def test_with_ngrok_absent_the_modal_shows_the_install_hint_and_the_local_link() -> None:
    async def go(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        modal = await open_panel(pilot)
        modal.query_one("#remote-on", Switch).toggle()
        await pilot.pause()
        assert app.remote.running  # locally on, no tunnel: PLAN §6 fallback, no crash
        status = shown(modal.query_one("#remote-status", Static))
        assert status == INSTALL_HINT
        assert "ngrok is not installed" in status and "ngrok config add-authtoken" in status
        link = shown(modal.query_one("#remote-link", Static))
        info = app.remote.info
        assert info is not None
        assert link == info.url_local == f"http://127.0.0.1:{app.remote._port}/r/{info.token}/"
        assert "local only" in shown(modal.query_one("#remote-state", Static))

    drive(go, tunnel=missing_ngrok)


def test_m_is_refused_while_focus_is_in_a_view_and_the_palette_lists_remote_control() -> None:
    async def go(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        names = [command.title for command in app.get_system_commands(app.screen)]
        assert "Remote control" in names
        app.query_one("#doctor").focus()
        await pilot.pause()
        await pilot.press("m")
        await pilot.pause()
        assert not isinstance(app.screen, RemotePanel), "m must not open the modal from a view"
        await pilot.press("question_mark")
        await pilot.pause()
        assert not isinstance(app.screen, HelpScreen)
        app.sidebar.focus()
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        help_screen = app.screen
        assert isinstance(help_screen, HelpScreen)
        assert "remote control" in shown(help_screen.query_one(Static))
        await pilot.press("escape")
        await pilot.pause()
        await open_panel(pilot)

    drive(go, tunnel=missing_ngrok)


# --- state survives a restart of the TUI ---------------------------------------------------------


def test_the_modal_state_survives_a_restart_of_the_tui() -> None:
    async def first(pilot: Pilot[None]) -> None:
        modal = await open_panel(pilot)
        modal.query_one("#remote-allow-write", Switch).toggle()
        await pilot.pause()
        modal.query_one("#remote-auto-off", Select).value = 120
        await pilot.pause()
        modal.query_one("#remote-on", Switch).toggle()
        await pilot.pause()
        app = pilot.app
        assert isinstance(app, FleetApp)
        assert app.remote.state.allow_write is True
        assert app.remote.state.auto_off_minutes == 120

    drive(first, tunnel=missing_ngrok)
    saved = json.loads(paths.state_path().read_text())
    assert (saved["remote_enabled"], saved["allow_write"], saved["auto_off_minutes"]) == (
        True,
        True,
        120,
    )

    async def second(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        assert app.remote.running, "a Remote that was on comes back on at the next start"
        modal = await open_panel(pilot)
        assert modal.query_one("#remote-on", Switch).value is True
        assert modal.query_one("#remote-allow-write", Switch).value is True
        assert modal.query_one("#remote-auto-off", Select).value == 120
        assert shown(modal.query_one("#remote-write-hint", Static)) == "writes reach the fleet"
        # The persisted token is the same one, so the link the phone kept still works.
        assert remote_server.runtime().token in shown(modal.query_one("#remote-link", Static))
        modal.query_one("#remote-allow-write", Switch).toggle()
        await pilot.pause()
        assert json.loads(paths.state_path().read_text())["allow_write"] is False

    drive(second, tunnel=missing_ngrok)
    # Leaving the TUI ends the processes but keeps the switch for the next restore.
    assert json.loads(paths.state_path().read_text())["remote_enabled"] is True
    assert remote_server.status()["running"] is False


def test_a_fresh_home_opens_with_write_actions_off() -> None:
    async def go(pilot: Pilot[None]) -> None:
        modal = await open_panel(pilot)
        assert modal.query_one("#remote-allow-write", Switch).value is False
        assert modal.query_one("#remote-on", Switch).value is False
        assert shown(modal.query_one("#remote-write-hint", Static)) == READ_ONLY_REASON

    drive(go, tunnel=missing_ngrok)
    assert not paths.state_path().exists() or "allow_write" not in json.loads(
        paths.state_path().read_text()
    )


# --- devices -----------------------------------------------------------------------------------


def test_devices_list_shows_sessions_from_remote_json_and_revoke_drops_one() -> None:
    async def go(pilot: Pilot[None]) -> None:
        modal = await open_panel(pilot)
        modal.query_one("#remote-on", Switch).toggle()
        await pilot.pause()
        runtime = remote_server.runtime()
        assert runtime.unlock(runtime.password, "iPhone Safari") is not None
        assert runtime.unlock(runtime.password, "Firefox") is not None
        assert runtime.unlock("wrong", "Burglar") is None
        assert len(sessions()) == 2
        assert json.loads(paths.remote_state_path().read_text())["allow_write"] is False
        modal.repaint()
        await pilot.pause()
        table = modal.query_one("#remote-devices")
        assert table.row_count == 2  # type: ignore[attr-defined]
        modal.query_one("#remote-revoke").press()  # type: ignore[attr-defined]
        await pilot.pause()
        left = sessions()
        assert len(left) == 1 and left[0]["ua"] == "Firefox"  # the cursor was on the first row
        assert table.row_count == 1  # type: ignore[attr-defined]

    drive(go, tunnel=missing_ngrok)


def test_qr_text_is_compact_half_block_art_of_the_url() -> None:
    art = qr_text("https://abcd-12.ngrok-free.app/r/AbCdEfGhIjKlMnOpQrStUv")
    rows = art.splitlines()
    assert 15 <= len(rows) <= 22, len(rows)
    assert all(set(row) <= set("█▀▄ ") for row in rows)
