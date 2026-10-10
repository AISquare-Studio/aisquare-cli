"""The ``R`` modal: opens from the sidebar, toggles Remote, shows the ngrok hint, survives restarts.

Driven headless with ``App.run_test`` at 140x40 as ``test_ui_shell.py`` drives the
shell. The server is the REAL ``services.remote_server`` on a free port (its
process-wide runtime is reset per test, as its own tests do); the tunnel
factory is scripted per test — ngrok is absent on the build machine, and
the acceptance says that case must be a sentence in the modal, not a crash.
Every assertion reads what a widget SHOWS.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import socket
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar

import pytest
from rich.color import Color
from textual.pilot import Pilot
from textual.widgets import Button, DataTable, Select, Static, Switch

from aisquare.cli.ui import app as app_mod
from aisquare.cli.ui import remote_control
from aisquare.cli.ui.app import FleetApp, HelpScreen
from aisquare.cli.ui.remote_control import READ_ONLY_REASON, RemoteController
from aisquare.cli.ui.views import remote as remote_view
from aisquare.cli.ui.views.remote import RemotePanel, qr_text
from aisquare.core import paths, state_file
from aisquare.core import tmux as tmux_core
from aisquare.core.locking import lock_exclusive, unlock
from aisquare.core.state_file import read_state, update_state
from aisquare.core.tmux import Completed
from aisquare.models import FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import remote_page, remote_server
from aisquare.services.ngrok_tunnel import INSTALL_HINT, NgrokTunnel, build_public_url
from aisquare.services.remote_server import UNLOCK_GLOBAL_FAILURES, Runtime, UnlockBudget
from tests.test_remote_control import FakeServer, FakeTunnel, SlowServer, fake_tunnel_factory

T = TypeVar("T")
SIZE = (140, 40)
PUBLIC = "https://abcd-12.ngrok-free.app"
PACIFIC = timezone(timedelta(hours=-7), "PDT")
"""A machine whose own zone is not UTC: the R panel's times are said in it."""
REAL_PUBLIC = "https://substantial-kestrel-92417.ngrok-free.dev"
"""A host the length ngrok really hands out: with ``/r/<32-char token>/`` the link is ~84
characters, which is what pushed Copy off the row (the short PUBLIC above never did)."""


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
    remote_server.stop_remote_server()


@pytest.fixture(autouse=True)
def installed_page(isolated_home: Path) -> Path:
    """Every test here starts from a machine where a page IS installed.

    A fresh machine would serve the page aisquare-cli bundles anyway; an
    installed one keeps these tests about the link, the QR and the password
    independent of it. The one test that wants no page at all deletes this
    directory and hides the bundled page itself.
    """
    dist = paths.remote_dist_dir()
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text("<!doctype html><title>asq remote</title>")
    return dist


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
    size: tuple[int, int] = SIZE,
) -> T:
    """Run ``fn`` against a mounted ``FleetApp`` whose Remote uses the stub server + ``tunnel``."""

    async def run() -> T:
        controller = RemoteController(
            server=remote_server, tunnel_factory=tunnel, url_timeout=2, port=free_port()
        )
        app = FleetApp(refresh_seconds=3600, doctor=lambda: [], remote=controller)
        async with app.run_test(size=size, notifications=True) as pilot:
            await pilot.pause()
            result = await fn(pilot)
        # Quit leaves a Remote stopping on its own thread; run_ui waits for it, and so must
        # the next test, whose server would be the one a late stop took down.
        assert controller.wait_until_off(10), "the Remote did not stop after quit"
        return result

    return asyncio.run(run())


def painted(app: FleetApp) -> list[str]:
    """Every row of the screen as the terminal would actually show it.

    Widget geometry is not the claim here: the Copy button that started this
    work WAS laid out, at x=135 inside a dialog ending at x=151, and painted
    nowhere. Only the composited strips answer "can the human see it".
    """
    update = app.screen._compositor.render_full_update()
    strips = update.strips
    strips = list(strips.values()) if isinstance(strips, dict) else list(strips)
    return [
        strip.text if hasattr(strip, "text") else "".join(getattr(s, "text", "") for s in strip)
        for strip in strips
    ]


def shown(widget: Static) -> str:
    visual = widget.visual
    plain = getattr(visual, "plain", None)
    assert isinstance(plain, str), f"{widget!r} renders a {type(visual).__name__}, not text"
    return plain


def toasts(app: FleetApp) -> list[str]:
    """The notifications the app still shows."""
    return [note.message for note in app._notifications]


def panel(pilot: Pilot[None]) -> RemotePanel:
    screen = pilot.app.screen
    assert isinstance(screen, RemotePanel), f"the top screen is a {type(screen).__name__}"
    return screen


async def open_panel(pilot: Pilot[None]) -> RemotePanel:
    await pilot.press("R")
    await pilot.pause()
    return panel(pilot)


async def written(pilot: Pilot[None]) -> None:
    """Let the writes of ``remote.json`` the panel asked for land on the controller's writer
    thread, what they hand back to Textual's thread run, and the panel paint them."""
    app = pilot.app
    assert isinstance(app, FleetApp)
    await pilot.pause()  # the press's handler asks for its writes as it runs
    for _ in range(500):
        if app.remote.writes_done(0):
            break
        await asyncio.sleep(0.01)
    assert app.remote.writes_done(0), "a write of remote.json never landed"
    # What a write handed Textual's thread (a toast, a saved switch) went to the loop with
    # call_soon_threadsafe before the write was done: one turn of the loop puts it in the
    # app's queue ahead of the markers the pause queues, so the pause waits for it.
    await asyncio.sleep(0)
    await pilot.pause()
    if isinstance(app.screen, RemotePanel):
        app.screen.repaint()
    await pilot.pause()


def devices() -> list[dict[str, str]]:
    listed = remote_server.remote_server_status()["devices"]
    assert isinstance(listed, list)
    return listed


def missing_ngrok(port: int) -> NgrokTunnel:
    return FakeTunnel(port, url=None, failure=INSTALL_HINT)


# --- open, toggle, close -------------------------------------------------------------------------


def test_shift_r_opens_the_remote_panel_and_the_switch_turns_remote_on_and_off() -> None:
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
        assert remote_server.remote_server_status()["running"] is True
        expected = build_public_url(PUBLIC, app.remote.info.token)  # type: ignore[union-attr]
        assert shown(modal.query_one("#remote-link", Static)) == expected
        assert shown(modal.query_one("#remote-state", Static)).startswith("on")
        password = shown(modal.query_one("#remote-password", Static))
        assert password == remote_server.runtime().password and len(password.split("-")) == 4
        qr = shown(modal.query_one("#remote-qr", Static))
        assert qr == qr_text(expected) and 15 <= len(qr.splitlines()) <= 22
        assert shown(modal.query_one("#remote-write-hint", Static)) == READ_ONLY_REASON
        assert modal.query_one("#remote-allow-write", Switch).value is False

        switch.toggle()
        await pilot.pause()
        assert not app.remote.running
        assert app.remote.wait_until_off(10)
        assert remote_server.remote_server_status()["running"] is False
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
        await written(pilot)
        assert app.remote.running  # locally on, no tunnel: PLAN §6 fallback, no crash
        status = shown(modal.query_one("#remote-status", Static))
        assert status == INSTALL_HINT
        assert "ngrok is not installed" in status and "ngrok config add-authtoken" in status
        link = shown(modal.query_one("#remote-link", Static))
        info = app.remote.info
        assert info is not None
        local = f"http://127.0.0.1:{app.remote._port}/r/{info.token}/"
        assert link == f"{info.url_local}\n{remote_view.LOCAL_ONLY}" and info.url_local == local
        assert "local only" in shown(modal.query_one("#remote-state", Static))
        assert shown(modal.query_one("#remote-qr", Static)) == "", "no QR a phone cannot open"

    drive(go, tunnel=missing_ngrok)


def test_no_qr_is_drawn_until_ngroks_link_is_up_nor_while_ngrok_restarts() -> None:
    """With no tunnel up the panel drew a scannable QR of the 127.0.0.1 link: a phone that
    scanned it got "cannot connect", leading to its own loopback, a first-time user without
    ngrok most of all (sweep of #243). The QR is ngrok's link's only; the local link says
    it is this machine's only."""
    tunnels: list[FakeTunnel] = []

    def late(port: int) -> NgrokTunnel:
        tunnels.append(FakeTunnel(port, url=None, failure=None))
        return tunnels[-1]

    async def go(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        modal = await open_panel(pilot)
        modal.query_one("#remote-on", Switch).toggle()
        await written(pilot)  # ngrok starts once the start's deadline is written
        info = app.remote.info
        assert info is not None
        qr, link = modal.query_one("#remote-qr", Static), modal.query_one("#remote-link", Static)
        assert shown(qr) == "", "starting ngrok: no QR of the local link"
        assert shown(link).startswith(f"{info.url_local}\n")
        tunnels[0].handle_line(json.dumps({"lvl": "info", "msg": "started tunnel", "url": PUBLIC}))
        modal.repaint()
        public = build_public_url(PUBLIC, info.token)
        assert shown(qr) == qr_text(public) and shown(link) == public

        app.remote.revive_tunnel_if_dead()  # a FakeTunnel never runs: it died
        modal.repaint()
        assert len(tunnels) == 2 and app.remote.public_url is None
        assert shown(qr) == "", "restarting ngrok: no QR of the local link"
        assert shown(link).startswith(f"{info.url_local}\n")

    drive(go, tunnel=late)


def test_a_link_ngrok_announces_after_the_wait_replaces_the_local_one_in_the_panel() -> None:
    """ngrok still retrying its session when the wait for its URL ended (a TUI started before
    the Wi-Fi was up): the URL it announced later never reached the panel, which kept the
    local link, its QR and "did not announce a tunnel in time" (r3 review of #243)."""
    tunnels: list[FakeTunnel] = []

    def late(port: int) -> NgrokTunnel:
        tunnels.append(FakeTunnel(port, url=None, failure=None))
        return tunnels[-1]

    async def go(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        modal = await open_panel(pilot)
        modal.query_one("#remote-on", Switch).toggle()
        await written(pilot)
        assert app.remote._waiter is not None
        app.remote._waiter.join(5)
        modal.repaint()
        info = app.remote.info
        assert info is not None
        status = modal.query_one("#remote-status", Static)
        assert shown(status) == "ngrok did not announce a tunnel in time"
        assert shown(modal.query_one("#remote-link", Static)).startswith(f"{info.url_local}\n")
        assert shown(modal.query_one("#remote-qr", Static)) == ""

        tunnels[0].handle_line(json.dumps({"lvl": "info", "msg": "started tunnel", "url": PUBLIC}))
        modal.repaint()
        await pilot.pause()
        expected = build_public_url(PUBLIC, info.token)
        assert shown(modal.query_one("#remote-link", Static)) == expected
        assert shown(modal.query_one("#remote-qr", Static)) == qr_text(expected)
        assert shown(status) == ""
        assert remote_server.runtime().remote_public_origin() == PUBLIC, "push links lead there"

    drive(go, tunnel=late)


def test_with_no_page_to_serve_the_modal_says_to_reinstall_and_remote_stays_off(
    installed_page: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``R``, Remote on, and nothing to serve: no installed page and no bundled one either.

    Before ``install-page`` existed this turned Remote ON against an empty
    ``~/.aisquare/remote-dist`` and the phone got a 404 with no explanation
    anywhere in the TUI. A fresh machine now serves the page aisquare-cli
    bundles; only an install that lost it gets here, and the switch comes back
    off with the status line saying what fixes it.
    """
    shutil.rmtree(installed_page)
    monkeypatch.setattr(remote_page, "bundled_page_files", dict)

    async def go(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        modal = await open_panel(pilot)
        modal.query_one("#remote-on", Switch).toggle()
        await pilot.pause()

        status = shown(modal.query_one("#remote-status", Static))
        assert "the bundled remote page is missing" in status
        assert "reinstall aisquare-cli" in status
        assert not app.remote.running
        assert app.remote.info is None
        assert remote_server.remote_server_status()["running"] is False
        # The switch snaps back and the saved state is untouched: no blind retry at next start.
        assert modal.query_one("#remote-on", Switch).value is False
        assert shown(modal.query_one("#remote-state", Static)) == "off"
        assert shown(modal.query_one("#remote-link", Static)) == "turn Remote on for a link"
        assert app.remote.state.remote_enabled is False

    drive(go, tunnel=missing_ngrok)
    # A refused start persists nothing at all: no state file was even created here.
    assert not paths.state_path().exists()


@pytest.mark.parametrize("size", [(155, 68), (100, 30)], ids=["real-terminal", "cramped"])
def test_copy_stays_visible_beside_a_real_length_ngrok_link(size: tuple[int, int]) -> None:
    """The demo's most important control must survive an ~84-character link.

    Sharing one row with the link, Copy was laid out past the dialog's right
    edge and painted nowhere — the human could see a link they had no way to
    copy. Copy now sits on the label row and the link has a line of its own.
    """

    async def go(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        modal = await open_panel(pilot)
        modal.query_one("#remote-on", Switch).toggle()
        await pilot.pause()
        for _ in range(20):  # the URL arrives from the tunnel's thread
            if app.remote.public_url is not None:
                break
            await asyncio.sleep(0.1)
        modal.repaint()
        await pilot.pause()

        link = app.remote.link_url()
        assert link is not None and len(link) >= 80, f"the test's link is too short: {link!r}"
        rows = painted(app)
        assert any("Copy" in row for row in rows), f"Copy is not painted at {size}"
        assert any(link in row for row in rows), f"the link is not painted whole at {size}"

        # And it copies THAT link, not a truncated or decorated version of it.
        modal.query_one("#remote-copy", Button).press()
        await pilot.pause()
        assert app.clipboard == link

    drive(go, tunnel=fake_tunnel_factory(url=REAL_PUBLIC), size=size)


def counted(controller: RemoteController) -> list[str]:
    """Every turn on, turn off and write-switch flip the panel asks of ``controller``."""
    calls: list[str] = []
    turn_on, turn_off, set_allow_write = (
        controller.turn_on,
        controller.turn_off,
        controller.set_allow_write,
    )

    def on(**kwargs: Any) -> None:
        calls.append("on")
        turn_on(**kwargs)

    def off(**kwargs: Any) -> bool:
        calls.append("off")
        return turn_off(**kwargs)

    def write(enabled: bool, **kwargs: Any) -> None:
        calls.append(f"write {enabled}")
        set_allow_write(enabled, **kwargs)

    controller.turn_on = on  # type: ignore[method-assign]
    controller.turn_off = off  # type: ignore[method-assign]
    controller.set_allow_write = write  # type: ignore[method-assign]
    return calls


async def settle(pilot: Pilot[None]) -> None:
    """Let every message in flight be handled, and any it posts in turn."""
    for _ in range(10):
        await pilot.pause()


def test_presses_in_flight_are_the_humans_net_word_and_never_flip_remote_for_ever() -> None:
    """Each value a repaint wrote back came round as a ``Changed`` the panel took for a
    press, so with two presses in flight they never ran out: Remote started and stopped
    uvicorn and ngrok, and revoked every phone, until the panel was closed; the write switch
    flipped hundreds of times a second (sweep of #243). Presses landing before the first is
    handled are one word, the last one's."""

    async def go(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        modal = await open_panel(pilot)
        calls = counted(app.remote)
        on = modal.query_one("#remote-on", Switch)
        on.toggle()
        on.toggle()
        on.toggle()  # three presses before the panel heard the first
        await settle(pilot)
        assert calls == ["on"] and app.remote.running and on.value is True

        writes = modal.query_one("#remote-allow-write", Switch)
        writes.toggle()
        writes.toggle()  # on and off again: nothing to do
        await settle(pilot)
        assert calls == ["on"] and writes.value is False
        assert app.remote.write_actions_allowed() is False

        on.toggle()
        on.toggle()
        on.toggle()
        on.toggle()  # off, on, off, on: still on
        await settle(pilot)
        await asyncio.sleep(0.3)
        modal.repaint()
        await settle(pilot)
        assert calls == ["on"] and app.remote.running and on.value is True

    drive(go, tunnel=missing_ngrok)


def test_a_tick_between_a_press_and_its_changed_keeps_the_press() -> None:
    """The one-second repaint landing after a press and before the panel heard of it wrote
    the controller's state back over the switch: the press was undone, or, with its echo,
    the switches flipped for ever."""

    async def go(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        modal = await open_panel(pilot)
        calls = counted(app.remote)
        on = modal.query_one("#remote-on", Switch)
        on.toggle()
        modal.repaint()  # the tick, before the press's Changed is handled
        assert on.value is True, "the switch keeps the press"
        await settle(pilot)
        assert calls == ["on"] and app.remote.running

        handled: list[str | None] = []
        repaint = modal.repaint

        def recording(*, heard: str | None = None) -> None:
            handled.append(heard)
            repaint(heard=heard)

        modal.repaint = recording  # type: ignore[method-assign]
        RemoteController.turn_off(app.remote)  # as auto-off would: the switch follows
        modal.repaint()
        await settle(pilot)
        assert on.value is False and calls == ["on"]
        assert handled == [None], "writing the switch posted a Changed the panel handled"

    drive(go, tunnel=missing_ngrok)


# --- auto-off: Never ------------------------------------------------------------------------


def test_never_is_selectable_stops_the_timer_and_says_so() -> None:
    """The human's server died 60 minutes into a session; Never is the way to stop that."""

    async def go(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        modal = await open_panel(pilot)
        modal.query_one("#remote-on", Switch).toggle()
        await pilot.pause()
        assert app.remote.auto_off_at is not None  # 60 minutes by default
        assert "auto-off at" in shown(modal.query_one("#remote-state", Static))

        modal.query_one("#remote-auto-off", Select).value = None
        await written(pilot)

        assert app.remote.state.auto_off_minutes is None
        assert app.remote.auto_off_at is None
        assert app.remote.running, "choosing Never must not turn Remote off"
        assert app.remote.enforce_auto_off() is False  # the timer can never fire now
        state_line = shown(modal.query_one("#remote-state", Static))
        assert "no auto-off" in state_line and "auto-off at" not in state_line
        # The server reports it the same way it reports "no timer" anywhere else (§4-B).
        assert json.loads(paths.remote_state_path().read_text())["auto_off_at"] is None

    drive(go, tunnel=missing_ngrok)


def test_never_survives_a_restart_while_a_fresh_machine_still_defaults_to_sixty() -> None:
    """Never persists as ``"never"``; an ABSENT key is a fresh machine and still means 60.

    The two must not be conflated: ``state.json`` with no auto-off key at all is
    a machine that has never chosen, and never-by-default would be the wrong
    resting posture for a page anyone with the link can reach.
    """

    async def fresh(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        assert app.remote.state.auto_off_minutes == 60
        modal = await open_panel(pilot)
        assert modal.query_one("#remote-auto-off", Select).value == 60
        modal.query_one("#remote-auto-off", Select).value = None
        await pilot.pause()

    drive(fresh, tunnel=missing_ngrok)
    assert json.loads(paths.state_path().read_text())["auto_off_minutes"] == "never"

    async def after_restart(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        assert app.remote.state.auto_off_minutes is None
        modal = await open_panel(pilot)
        assert modal.query_one("#remote-auto-off", Select).value is None
        assert "Never" in "".join(painted(app))
        # …and a timer can still be chosen again afterwards.
        modal.query_one("#remote-auto-off", Select).value = 30
        await pilot.pause()
        assert app.remote.state.auto_off_minutes == 30

    drive(after_restart, tunnel=missing_ngrok)
    assert json.loads(paths.state_path().read_text())["auto_off_minutes"] == 30


def test_m_is_refused_while_focus_is_in_a_view_and_the_palette_lists_remote_control() -> None:
    async def go(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        names = [command.title for command in app.get_system_commands(app.screen)]
        assert "Remote control" in names
        app.query_one("#doctor").focus()
        await pilot.pause()
        await pilot.press("R")
        await pilot.pause()
        assert not isinstance(app.screen, RemotePanel), "R must not open the modal from a view"
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


def test_r_is_off_the_footer_so_the_footer_fits_80_columns() -> None:
    """The footer is the one row every screen of the shell shares, and at 80 columns
    "R remote" pushed the keys after it off the edge. ``R`` still opens the panel, and
    the help screen and the palette list it (the test above)."""

    async def go(pilot: Pilot[None]) -> str:
        app = pilot.app
        assert isinstance(app, FleetApp)
        app.sidebar.focus()
        await pilot.pause()
        footer = painted(app)[-1]
        await open_panel(pilot)
        return footer

    footer = drive(go, tunnel=missing_ngrok, size=(80, 24))
    assert "help" in footer, f"the last row is the footer: {footer!r}"
    assert "remote" not in footer


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
        assert app.remote.write_actions_allowed() is True
        assert app.remote.state.auto_off_minutes == 120

    drive(first, tunnel=missing_ngrok)
    saved = json.loads(paths.state_path().read_text())
    assert (saved["remote_enabled"], saved["auto_off_minutes"]) == (True, 120)
    # The write switch is remote.json's alone: flipped before Remote was on, it landed there.
    assert json.loads(paths.remote_state_path().read_text())["allow_write"] is True

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
        await written(pilot)
        assert json.loads(paths.remote_state_path().read_text())["allow_write"] is False

    drive(second, tunnel=missing_ngrok)
    # Leaving the TUI ends the processes but keeps the switch for the next restore.
    assert json.loads(paths.state_path().read_text())["remote_enabled"] is True
    assert remote_server.remote_server_status()["running"] is False


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


def test_the_panels_first_paint_never_waits_for_another_processs_lock_on_remote_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The panel's first paint was the process's first read of ``remote.json``, which makes
    a missing file under ``remote.json.lock``: on Textual's thread, the fleet UI froze for
    the 2 s another process held it (sweep 3 of #243). It opens at once, and paints the file
    once the writer's thread has read it."""
    monkeypatch.setattr(remote_server, "STATE_LOCK_WAIT_SECONDS", 10.0)
    state = paths.remote_state_path()
    state.parent.mkdir(parents=True, exist_ok=True)
    held = os.open(state.with_name(f"{state.name}.lock"), os.O_RDWR | os.O_CREAT, 0o600)

    async def go(pilot: Pilot[None]) -> tuple[float, bool]:
        lock_exclusive(held)  # another process, in the middle of its write
        try:
            started = time.monotonic()
            modal = await open_panel(pilot)
            took = time.monotonic() - started
            made_meanwhile = state.exists()
        finally:
            unlock(held)
            os.close(held)
        await written(pilot)
        assert modal.query_one("#remote-allow-write", Switch).value is False
        assert modal.controller.read_problem is None
        return took, made_meanwhile

    took, made_meanwhile = drive(go, tunnel=missing_ngrok)
    assert took < 1.5, f"the panel took {took:.1f} s to open"
    assert not made_meanwhile and state.exists() and remote_server.remote_state_loaded()


# --- devices -----------------------------------------------------------------------------------


def test_devices_list_shows_devices_from_remote_json_and_revoke_drops_one() -> None:
    async def go(pilot: Pilot[None]) -> None:
        modal = await open_panel(pilot)
        modal.query_one("#remote-on", Switch).toggle()
        await pilot.pause()
        runtime = remote_server.runtime()
        assert runtime.unlock_device(runtime.password, "iPhone Safari") is not None
        assert runtime.unlock_device(runtime.password, "Firefox") is not None
        assert runtime.unlock_device("wrong", "Burglar") is None
        assert len(devices()) == 2
        assert json.loads(paths.remote_state_path().read_text())["allow_write"] is False
        modal.repaint()
        await pilot.pause()
        table = modal.query_one("#remote-devices")
        assert table.row_count == 2  # type: ignore[attr-defined]
        modal.query_one("#remote-revoke").press()  # type: ignore[attr-defined]
        await written(pilot)
        left = devices()
        assert len(left) == 1 and left[0]["ua"] == "Firefox"  # the cursor was on the first row
        assert table.row_count == 1  # type: ignore[attr-defined]
        app = pilot.app
        assert isinstance(app, FleetApp)
        assert any(text.startswith("Revoked dev_") for text in toasts(app)), "the control"

    drive(go, tunnel=missing_ngrok)


def test_the_devices_table_follows_last_seen_and_sign_in_while_the_devices_stay_the_same(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The table was built again only when a device came or went: a phone back on kept its
    old "last seen", and one signed out after a day idle still read "signed in", the very
    columns a revoke is decided from (r2 review of #243). The cells change in place, and
    the cursor stays on the row the user put it on."""
    monkeypatch.setattr(remote_view, "LOCAL_ZONE", PACIFIC)

    async def go(pilot: Pilot[None]) -> None:
        modal = await open_panel(pilot)
        modal.query_one("#remote-on", Switch).toggle()
        await pilot.pause()
        runtime = remote_server.runtime()
        iphone = runtime.unlock_device(runtime.password, "iPhone Safari")
        assert iphone is not None and runtime.unlock_device(runtime.password, "Firefox")
        modal.repaint()
        await pilot.pause()
        table = modal.query_one("#remote-devices", DataTable)

        def rows() -> list[list[str]]:
            return [[str(cell) for cell in table.get_row_at(row)] for row in range(table.row_count)]

        first = rows()
        assert [row[1] for row in first] == ["iPhone Safari", "Firefox"]
        assert [row[4] for row in first] == ["signed in", "signed in"]
        table.move_cursor(row=1)
        await pilot.pause()

        clock = [datetime.now(UTC) + timedelta(hours=2)]
        monkeypatch.setattr(remote_server, "_remote_now", lambda: clock[0])
        assert runtime.device_for_cookie(iphone[0]) is not None  # the iPhone is back on
        modal.repaint()
        await pilot.pause()
        back = rows()
        here = clock[0].astimezone(PACIFIC)
        seen = f"{here:%b} {here.day} {here:%H:%M}"  # the machine's zone, the date kept
        assert back[0][2] == seen != first[0][2]
        assert back[1] == first[1]

        clock[0] += timedelta(hours=23)  # Firefox is a day idle now, the iPhone 23 h
        modal.repaint()
        await pilot.pause()
        idle = rows()
        assert [row[4] for row in idle] == ["signed in", "signed out"]
        assert [row[0] for row in idle] == [row[0] for row in first]
        assert table.cursor_row == 1, "the cursor stays on the row the user put it on"

    drive(go, tunnel=missing_ngrok)


def test_a_devices_times_are_said_in_this_machines_zone_with_their_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cells showed the server's UTC stamps cut to 19 characters, the offset and the
    seconds' last digit gone: in Los Angeles at 20:58 a phone seen that second read
    ``2026-10-08T03:58:0…``, tomorrow, two rows under "auto-off at 21:58", and its sign-in,
    ending Oct 14 at 20:58, read Oct 15 (sweep of #243). They read as that line does, and
    keep the date: a sign-in ends a week on, on the same weekday."""
    monkeypatch.setattr(remote_view, "LOCAL_ZONE", PACIFIC)
    row = {
        "id": "dev_448c5fba",
        "ua": "iPhone Safari",
        "last_seen": "2026-10-08T03:58:01+00:00",
        "expires_at": "2026-10-15T03:58:01+00:00",
        "signed_in": True,
    }
    assert remote_view._device_cells(row) == (
        "dev_448c5fba",
        "iPhone Safari",
        "Oct 7 20:58",
        "Oct 14 20:58",
        "signed in",
    )
    hand_edited = {"id": "dev_1", "last_seen": "2026-10-08T03:58:01", "expires_at": "soon"}
    assert remote_view._device_cells(hand_edited)[2:4] == ("Oct 7 20:58", "soon"), (
        "a stamp without its offset is UTC, as the server reads it; no time shows as it came"
    )
    assert remote_view._device_cells({"id": "dev_2"})[2:4] == ("—", "—")


def test_a_repaint_reads_the_status_once_and_draws_the_qr_only_for_a_new_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every one-second repaint encoded the QR anew, about 4 ms of segno on Textual's own
    thread, and read ``remote_server_status()`` twice, each read three digests of
    ``remote.json`` (r2 review of #243). The QR is drawn again when the link changes, and
    only for ngrok's link."""
    drawn: list[str] = []
    reads: list[None] = []
    status = remote_server.remote_server_status

    def drawing(url: str) -> str:
        drawn.append(url)
        return qr_text(url)

    def reading() -> dict[str, object]:
        reads.append(None)
        return status()

    monkeypatch.setattr(remote_view, "qr_text", drawing)
    monkeypatch.setattr(remote_server, "remote_server_status", reading)

    async def go(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        modal = await open_panel(pilot)
        modal.query_one("#remote-on", Switch).toggle()
        await pilot.pause()
        info = app.remote.info
        assert info is not None and drawn == [], "no QR of the local link"
        statuses = len(reads)
        modal.repaint()
        modal.repaint()
        assert len(reads) == statuses + 2, "one status read a repaint"

        app.remote.public_origin = PUBLIC  # ngrok announced it
        public = build_public_url(PUBLIC, info.token)
        assert app.remote.public_url == public
        modal.repaint()
        assert drawn == [public]
        modal.repaint()
        modal.repaint()
        assert drawn == [public], "the same link: no QR drawn again"
        await pilot.pause()
        assert shown(modal.query_one("#remote-qr", Static)) == qr_text(public)
        assert shown(modal.query_one("#remote-link", Static)) == public

    drive(go, tunnel=missing_ngrok)


def brightness(color: Color | None) -> float:
    assert color is not None, "a QR cell painted in no colour of its own"
    red, green, blue = color.get_truecolor()
    return 0.299 * red + 0.587 * green + 0.114 * blue


@pytest.mark.parametrize("theme", ["textual-dark", "textual-light", "solarized-light"])
def test_the_qr_is_light_on_dark_in_every_theme(theme: str) -> None:
    """segno's compact art draws the light modules as glyphs, so in a light theme's colours
    the QR came out reflectance-reversed, its quiet zone a dark frame, and a scanner without
    inversion support could not read it (sweep of #243). Every glyph of it is painted
    lighter than the ground it stands on, whatever the theme."""

    async def go(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        app.theme = theme
        modal = await open_panel(pilot)
        modal.query_one("#remote-on", Switch).toggle()
        await written(pilot)
        assert app.remote._waiter is not None
        app.remote._waiter.join(5)  # the QR is ngrok's link's: it comes with it
        modal.repaint()
        await pilot.pause()
        qr = modal.query_one("#remote-qr", Static)
        region = qr.region
        assert region.height >= 15, "the QR is on screen"
        update = app.screen._compositor.render_full_update(simplify=True)
        rows = [strip for (strip,) in update.strips]  # simplified: one strip a row
        glyphs = 0
        for row in rows[region.y : region.y + region.height]:
            x = 0
            for segment in row:
                inside = region.x <= x < region.x + region.width
                x += len(segment.text)
                if not inside or not set(segment.text) & set("█▀▄") or segment.style is None:
                    continue
                glyphs += 1
                style = segment.style
                assert brightness(style.color) > brightness(style.bgcolor), (theme, style)
        assert glyphs > 10

    drive(go, tunnel=fake_tunnel_factory(url=PUBLIC), size=(160, 100))


def test_qr_text_is_compact_half_block_art_of_the_url() -> None:
    art = qr_text("https://abcd-12.ngrok-free.app/r/AbCdEfGhIjKlMnOpQrStUv")
    rows = art.splitlines()
    assert 15 <= len(rows) <= 22, len(rows)
    assert all(set(row) <= set("█▀▄ ") for row in rows)


def test_a_user_agent_that_is_rich_markup_is_painted_as_text() -> None:
    """A str cell is parsed as Rich markup: a User-Agent of ``x [/b]`` raised MarkupError
    when the devices table painted, which took the whole TUI down (review of #243)."""

    async def go(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        modal = await open_panel(pilot)
        modal.query_one("#remote-on", Switch).toggle()
        await pilot.pause()
        runtime = remote_server.runtime()
        assert runtime.unlock_device(runtime.password, "x [/b] [bold]phone") is not None
        modal.repaint()
        await pilot.pause()
        assert any("x [/b] [bold]phone" in row for row in painted(app))

    drive(go, tunnel=missing_ngrok, size=(160, 100))  # tall enough that the table is painted


def test_a_status_sentence_that_reads_as_markup_is_painted_as_it_is() -> None:
    """The status line's sentences carry exception text and paths: painted as a str they were
    markup, a ``[b]`` in a path a tag that vanished and a ``[/b]`` a ``MarkupError`` raised
    out of the repaint, which a one-second timer runs."""

    async def go(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        modal = await open_panel(pilot)
        sentence = "/home/[b]x[/b]/remote.json could not be written: [/i] is read-only"
        app.remote.message = sentence
        modal.repaint()
        await pilot.pause()
        assert shown(modal.query_one("#remote-status", Static)) == sentence

    drive(go, tunnel=missing_ngrok)


def test_the_link_and_its_qr_follow_a_new_link_from_another_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``regenerate-password --new-link`` from a shell retires the old link at the server's
    next request; the panel showed it still, and drew its QR, beside the new passphrase,
    until Remote was turned off and on, which signs every phone out (review of #243, round
    6). The link row and the QR follow the token ``remote.json`` holds, the local link too."""
    drawn: list[str] = []

    def drawing(url: str) -> str:
        drawn.append(url)
        return qr_text(url)

    monkeypatch.setattr(remote_view, "qr_text", drawing)

    async def go(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        modal = await open_panel(pilot)
        modal.query_one("#remote-on", Switch).toggle()
        await written(pilot)
        info = app.remote.info
        assert info is not None
        link = modal.query_one("#remote-link", Static)
        shell = Runtime(paths.remote_state_path(), paths.remote_audit_path())
        shell.regenerate_password(new_link=True)
        new = shell.token
        assert new != info.token
        modal.repaint()
        await pilot.pause()
        assert shown(link).splitlines()[0] == info.url_local.replace(info.token, new)
        assert drawn == [], "the local link has no QR"
        app.remote.public_origin = PUBLIC  # ngrok announced it
        modal.repaint()
        await pilot.pause()
        assert drawn == [build_public_url(PUBLIC, new)] and shown(link) == drawn[0]
        shell.regenerate_password(new_link=True)
        newer = shell.token
        modal.repaint()
        await pilot.pause()
        assert drawn[-1] == build_public_url(PUBLIC, newer) and shown(link) == drawn[-1]
        assert shown(modal.query_one("#remote-qr", Static)) == qr_text(drawn[-1])
        assert info.token not in shown(link) and new not in shown(link)

    drive(go, tunnel=missing_ngrok)


def test_the_password_shown_follows_a_regenerate_from_another_shell() -> None:
    """The modal showed the passphrase Remote started with, which a ``regenerate-password``
    from a shell had already made wrong (review of #243)."""

    async def go(pilot: Pilot[None]) -> None:
        modal = await open_panel(pilot)
        modal.query_one("#remote-on", Switch).toggle()
        await pilot.pause()
        shell = Runtime(paths.remote_state_path(), paths.remote_audit_path())
        fresh = shell.regenerate_password()
        modal.repaint()
        await pilot.pause()
        assert shown(modal.query_one("#remote-password", Static)) == fresh

    drive(go, tunnel=missing_ngrok)


def test_the_write_switch_is_remote_jsons_whatever_an_older_state_json_says() -> None:
    """``turn_on`` pushed the TUI's saved copy into the server: a TUI start undid an
    ``aisquare remote allow-write off`` from a shell, and the modal showed read-only while
    the server took writes after an ``allow-write on`` (review of #243)."""
    update_state("remote_enabled", True)
    update_state("allow_write", True)  # what an earlier build saved beside the theme
    shell = Runtime(paths.remote_state_path(), paths.remote_audit_path())
    shell.set_allow_write(False)

    async def go(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        assert app.remote.running, "restored"
        assert json.loads(paths.remote_state_path().read_text())["allow_write"] is False
        modal = await open_panel(pilot)
        assert modal.query_one("#remote-allow-write", Switch).value is False
        shell.set_allow_write(True)  # `aisquare remote allow-write on` in another shell
        modal.repaint()
        await pilot.pause()
        assert modal.query_one("#remote-allow-write", Switch).value is True
        assert shown(modal.query_one("#remote-write-hint", Static)) == "writes reach the fleet"

    drive(go, tunnel=missing_ngrok)


def refuse_remote_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """From now on ``remote.json`` cannot be replaced, as in a read-only or full home."""

    def refuse(path: Path, **kwargs: object) -> object:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(remote_server, "replacement", refuse)


def test_a_remote_json_that_will_not_write_leaves_the_ui_up_at_start_and_remote_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Remote that was on comes back in ``on_mount``, and the deadline it could not write
    raised out of it: ``asq ui`` ended at start, uvicorn still serving (r2 review of #243)."""
    update_state("remote_enabled", True)
    Runtime(paths.remote_state_path(), paths.remote_audit_path())  # an earlier Remote's file
    refuse_remote_json(monkeypatch)

    async def go(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        await written(pilot)
        assert not app.remote.running
        assert app.remote.wait_until_off(10), "the server it started stops on its own thread"
        assert remote_server.remote_server_status()["running"] is False
        modal = await open_panel(pilot)
        assert modal.query_one("#remote-on", Switch).value is False
        status = shown(modal.query_one("#remote-status", Static))
        assert status.startswith("Remote could not start — remote.json could not be written")

    drive(go, tunnel=missing_ngrok)


def test_a_remote_json_that_will_not_write_is_a_sentence_for_each_control_of_the_panel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The switch, the Auto-off picker, Regenerate and Revoke raised into Textual's handlers,
    and the exception ended the fleet UI while Remote kept serving (r2 review of #243). The
    write switch said it "could not be changed" while it showed the change phones now got."""

    async def go(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        modal = await open_panel(pilot)
        modal.query_one("#remote-on", Switch).toggle()
        await written(pilot)  # the start's deadline lands before remote.json refuses
        runtime = remote_server.runtime()
        assert runtime.unlock_device(runtime.password, "iPhone Safari") is not None
        modal.repaint()
        await pilot.pause()
        refuse_remote_json(monkeypatch)

        def status() -> str:
            return shown(modal.query_one("#remote-status", Static))

        modal.query_one("#remote-auto-off", Select).value = 30
        await written(pilot)
        assert "\nauto-off could not be saved to remote.json — [Errno 13]" in status()
        modal.query_one("#remote-revoke", Button).press()
        await written(pilot)
        assert "could not be revoked in remote.json" in status()
        assert not any(text.startswith("Revoked") for text in toasts(app)), "the revoke failed"
        modal.query_one("#remote-regen", Button).press()
        await written(pilot)
        assert "\nthe new password could not be saved to remote.json" in status()
        modal.query_one("#remote-allow-write", Switch).toggle()
        await written(pilot)
        assert "\nwrite actions could not be saved to remote.json — [Errno 13]" in status()
        assert modal.query_one("#remote-allow-write", Switch).value is True
        assert remote_server.remote_allow_write() is True, "the running server took it"
        assert app.screen is modal and app.remote.running

        modal.query_one("#remote-on", Switch).toggle()  # off still goes off
        await pilot.pause()
        assert not app.remote.running
        assert app.remote.wait_until_off(10), "the server and ngrok stop on their own thread"
        modal.repaint()
        assert remote_server.remote_server_status()["running"] is False
        assert "devices could not be revoked" in status()

        modal.query_one("#remote-on", Switch).toggle()  # and on does not stay on without it
        await written(pilot)
        assert app.screen is modal and not app.remote.running
        assert modal.query_one("#remote-on", Switch).value is False
        assert status().startswith("Remote could not start — remote.json could not be written")
        assert app.remote.wait_until_off(10)
        assert remote_server.remote_server_status()["running"] is False

    drive(go, tunnel=missing_ngrok)


def test_a_remote_json_that_cannot_be_read_is_a_sentence_in_the_panel_not_a_silence() -> None:
    """A ``remote.json`` that is no JSON object (a hand edit's typo) showed as writes off and
    no devices, and the panel said why only once Remote was switched on: a cause dropped, as
    ngrok's own plain-text errors were (sweep of #243). The status line says it as the panel
    paints, until the file can be read."""
    paths.ensure_home()
    paths.remote_state_path().write_text("[]")

    async def go(pilot: Pilot[None]) -> None:
        modal = await open_panel(pilot)
        await written(pilot)  # the process's first read is the writer thread's
        status = modal.query_one("#remote-status", Static)
        said = shown(status)
        assert said.startswith("remote.json could not be read — ")
        assert "is not a JSON object" in said
        assert modal.query_one("#remote-allow-write", Switch).value is False
        paths.remote_state_path().unlink()  # moved aside: a new link and passphrase
        modal.repaint()  # asks the writer's thread to read it again
        await written(pilot)
        assert shown(status) == ""

    drive(go, tunnel=missing_ngrok)


def test_a_write_that_lands_takes_away_the_sentence_that_one_did_not_in_the_panel() -> None:
    """The panel said write actions had not been saved for as long as Remote stayed on, after
    the switch, flipped back, had saved them (sweep of #243)."""

    async def go(pilot: Pilot[None]) -> None:
        modal = await open_panel(pilot)
        modal.query_one("#remote-on", Switch).toggle()
        await written(pilot)  # the start's deadline lands before remote.json refuses
        status = modal.query_one("#remote-status", Static)
        writes = modal.query_one("#remote-allow-write", Switch)
        with pytest.MonkeyPatch.context() as home:
            refuse_remote_json(home)
            writes.toggle()
            await written(pilot)
            assert shown(status).startswith(f"{INSTALL_HINT}\nwrite actions could not be saved")
        writes.toggle()  # off again, and this write lands
        await written(pilot)
        assert json.loads(paths.remote_state_path().read_text())["allow_write"] is False
        modal.repaint()
        assert shown(status) == INSTALL_HINT, "what Remote is doing stays"

    drive(go, tunnel=missing_ngrok)


def test_the_modal_shows_failed_unlocks_and_a_deadline_a_phone_extended() -> None:
    async def go(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        modal = await open_panel(pilot)
        modal.query_one("#remote-on", Switch).toggle()
        await written(pilot)
        assert shown(modal.query_one("#remote-unlocks", Static)) == ""
        runtime = remote_server.runtime()
        budget = UnlockBudget(runtime)
        for _ in range(UNLOCK_GLOBAL_FAILURES):
            budget.record_failed_unlock()
        extended = runtime.extend_auto_off(datetime.now(UTC))
        assert extended is not None
        modal.repaint()
        await pilot.pause()
        line = shown(modal.query_one("#remote-unlocks", Static))
        assert line.startswith(f"{UNLOCK_GLOBAL_FAILURES} failed unlocks in 30 min")
        assert "new unlocks paused" in line and "regenerate-password --new-link" in line
        state_line = shown(modal.query_one("#remote-state", Static))
        assert f"auto-off at {extended.astimezone():%H:%M}" in state_line
        assert app.remote.auto_off_at == extended, "the extension holds in the TUI too"

    drive(go, tunnel=missing_ngrok)


def test_the_switches_save_off_textuals_thread_and_land_once_the_lock_is_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The R panel saved Remote's switches to ``state.json`` on Textual's thread: with the
    file's lock held by another process (a second ``asq ui`` saving its theme, a ``project
    switch``), the fleet UI froze for the lock's wait on every switch (r3 review of #243:
    nothing that stops or saves Remote may hold that thread). They save as the theme does,
    and what quit could not land is said after."""
    monkeypatch.setattr(state_file, "LOCK_WAIT_S", 10.0)  # a holder that takes its time
    update_state("board_theme", "nord")  # the lock file

    async def go(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        modal = await open_panel(pilot)
        fd = os.open(paths.state_path().with_name("state.json.lock"), os.O_RDONLY)
        lock_exclusive(fd)
        try:
            started = time.monotonic()
            modal.query_one("#remote-on", Switch).toggle()
            await pilot.pause()
            assert time.monotonic() - started < 5.0, "the switch waited for state.json's lock"
            assert app.remote.running
            assert "remote_enabled" not in read_state(), "not saved while the lock is held"
        finally:
            unlock(fd)
            os.close(fd)
        for _ in range(100):
            if read_state().get("remote_enabled") is True:
                break
            await asyncio.sleep(0.05)
        assert read_state()["remote_enabled"] is True, "saved once the lock was free"
        modal.query_one("#remote-auto-off", Select).value = 120
        await pilot.pause()  # picked, and quit inside the save's debounce: quit lands it

    drive(go, tunnel=missing_ngrok)
    assert read_state() == {"board_theme": "nord", "remote_enabled": True, "auto_off_minutes": 120}


def test_the_panels_controls_never_wait_for_remote_jsons_lock_on_textuals_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every control of the panel wrote ``remote.json`` on Textual's thread, waiting for its
    lock: with another process holding it (a ``^Z``'d ``asq remote revoke``, a lock on NFS)
    each press froze the fleet UI for the lock's two seconds, nearly four behind the
    server's own flush (sweep of #243). The presses return at once, the status line says a
    write waits, and every write lands once the lock is let go."""
    monkeypatch.setattr(remote_server, "STATE_LOCK_WAIT_SECONDS", 10.0)  # a holder that waits

    async def go(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        modal = await open_panel(pilot)
        runtime = remote_server.runtime()
        assert runtime.unlock_device(runtime.password, "iPhone Safari") is not None
        passphrase = runtime.password
        fd = os.open(paths.remote_state_path().with_name("remote.json.lock"), os.O_RDWR)
        lock_exclusive(fd)
        try:
            presses: list[Callable[[], object]] = [
                lambda: modal.query_one("#remote-on", Switch).toggle(),
                lambda: modal.query_one("#remote-allow-write", Switch).toggle(),
                lambda: setattr(modal.query_one("#remote-auto-off", Select), "value", 120),
                lambda: modal.query_one("#remote-regen", Button).press(),
                lambda: modal.query_one("#remote-revoke", Button).press(),
            ]
            for press in presses:
                started = time.monotonic()
                press()
                await pilot.pause()
                assert time.monotonic() - started < 1.0, "a press waited for remote.json's lock"
            assert app.remote.running
            await asyncio.sleep(remote_control.SAVING_AFTER)
            modal.repaint()
            status = shown(modal.query_one("#remote-status", Static))
            assert remote_control.SAVING in status.splitlines()
            assert modal.query_one("#remote-allow-write", Switch).value is True
        finally:
            unlock(fd)
            os.close(fd)
        await written(pilot)
        saved = json.loads(paths.remote_state_path().read_text())
        assert saved["allow_write"] is True
        assert saved["auto_off_at"] is not None
        assert saved["password"] != passphrase and saved["devices"] == []
        assert remote_control.SAVING not in shown(modal.query_one("#remote-status", Static))
        assert "New password — every device has to unlock again" in toasts(app)

    drive(go, tunnel=missing_ngrok)


def test_the_panel_says_remote_is_on_while_another_process_serves_this_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``asq remote serve`` (or another fleet UI) serving this home publicly, the panel
    said "off", "turn Remote on for a link" and no passphrase, beside that Remote's phones
    listed as signed in and a write switch that flipped its writes: a human checking whether
    the fleet was exposed read that it was not (sweep of #243). It says another process
    serves this home, and stops saying so, and why a start failed, once it does not."""
    monkeypatch.setattr(remote_control, "ELSEWHERE_EVERY_SECONDS", 0.0)
    paths.ensure_home()
    serving = paths.remote_state_path().with_name(remote_server.SERVE_LOCK_NAME)

    async def go(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        passphrase = remote_server.runtime().password
        fd = os.open(serving, os.O_RDWR | os.O_CREAT, 0o600)
        lock_exclusive(fd)  # another process's Remote, on this home
        try:
            modal = await open_panel(pilot)
            state = modal.query_one("#remote-state", Static)
            for _ in range(100):
                modal.repaint()
                if shown(state) != "off":
                    break
                await asyncio.sleep(0.02)
            assert shown(state) == f"{remote_view.ELSEWHERE}  · no auto-off"
            assert shown(modal.query_one("#remote-link", Static)) == remote_view.ELSEWHERE_LINK
            assert shown(modal.query_one("#remote-password", Static)) == passphrase
            modal.query_one("#remote-on", Switch).toggle()
            await written(pilot)
            assert not app.remote.running
            status = modal.query_one("#remote-status", Static)
            assert shown(status) == remote_control.ALREADY_ON
        finally:
            unlock(fd)
            os.close(fd)
        for _ in range(100):
            modal.repaint()
            if shown(state) == "off":
                break
            await asyncio.sleep(0.02)
        assert shown(state) == "off"
        assert shown(status) == "", "the home is free: the start may be tried again"
        assert shown(modal.query_one("#remote-link", Static)) == "turn Remote on for a link"

    drive(go, tunnel=missing_ngrok)


@pytest.mark.parametrize("minutes", [45, None], ids=["a timer", "never"])
def test_while_another_process_serves_the_panel_shows_its_auto_off_and_picks_none(
    monkeypatch: pytest.MonkeyPatch, minutes: int | None
) -> None:
    """The Auto-off picker beside "on in another process" showed this UI's saved 60 min, and a
    pick of it was taken without a word while the serving Remote kept its own deadline, or
    none at all with ``serve --auto-off 0`` (sweep 3 of #243). The state says that Remote's
    timer, and the picker is off until this UI's Remote is the one to set."""
    monkeypatch.setattr(remote_control, "ELSEWHERE_EVERY_SECONDS", 0.0)
    monkeypatch.setattr(remote_view, "LOCAL_ZONE", UTC)
    paths.ensure_home()
    serving = paths.remote_state_path().with_name(remote_server.SERVE_LOCK_NAME)
    deadline = None if minutes is None else datetime(2026, 10, 9, 21, 58, tzinfo=UTC)
    said = "no auto-off" if deadline is None else "auto-off at 21:58"

    async def go(pilot: Pilot[None]) -> None:
        remote_server.set_auto_off(deadline)  # the other process's serve set it
        fd = os.open(serving, os.O_RDWR | os.O_CREAT, 0o600)
        lock_exclusive(fd)
        try:
            modal = await open_panel(pilot)
            await written(pilot)
            state = modal.query_one("#remote-state", Static)
            for _ in range(100):
                modal.repaint()
                if shown(state) != "off":
                    break
                await asyncio.sleep(0.02)
            assert shown(state) == f"{remote_view.ELSEWHERE}  · {said}"
            assert modal.query_one("#remote-auto-off", Select).disabled
        finally:
            unlock(fd)
            os.close(fd)
        for _ in range(100):
            modal.repaint()
            if shown(state) == "off":
                break
            await asyncio.sleep(0.02)
        assert not modal.query_one("#remote-auto-off", Select).disabled

    drive(go, tunnel=missing_ngrok)


def test_a_switch_state_json_refuses_is_toasted_and_said_again_after_quit() -> None:
    """A refused save of a switch was dropped without a word, and a refused off brought Remote
    back at the next start (sweep of #243). It is said as a refused theme is: a toast, and a
    line once the screen is gone."""
    paths.ensure_home()
    paths.state_path().write_text("[]")  # not an object: every save of it is refused

    async def go(pilot: Pilot[None]) -> FleetApp:
        app = pilot.app
        assert isinstance(app, FleetApp)
        modal = await open_panel(pilot)
        modal.query_one("#remote-on", Switch).toggle()
        await pilot.pause()
        for _ in range(100):
            if any("Remote's on/off switch could not be saved" in note for note in toasts(app)):
                break
            await asyncio.sleep(0.05)
        assert any(
            "is not a JSON object — Remote's on/off switch could not be saved" in note
            for note in toasts(app)
        )
        return app

    app = drive(go, tunnel=missing_ngrok)
    assert any(line.startswith("Remote's on/off switch was not saved: ") for line in app.unsaved)


# --- turning Remote off never waits on Textual's thread (r3 review of #243) ----------------------


def drive_controller(
    fn: Callable[[Pilot[None]], Awaitable[T]], controller: RemoteController
) -> tuple[T, float]:
    """``drive`` for a controller the test built; also how long leaving the app took."""

    async def run() -> tuple[T, float]:
        app = FleetApp(refresh_seconds=3600, doctor=lambda: [], remote=controller)
        async with app.run_test(size=SIZE, notifications=True) as pilot:
            await pilot.pause()
            result = await fn(pilot)
            leaving = time.monotonic()
        return result, time.monotonic() - leaving

    return asyncio.run(run())


def test_the_switch_and_the_auto_off_timer_turn_remote_off_without_freezing_the_ui() -> None:
    """Stopping uvicorn waits for a needs scan in flight and the push sender, then ngrok is
    given its seconds to exit: on Textual's thread, the switch and the 30 s auto-off timer
    froze the fleet UI for all of it. The panel reads off at once, says Remote is turning
    off, and says what went wrong, if anything, once the stopping is done."""
    server = SlowServer(patience=10.0)
    clock = [datetime.now(UTC)]
    controller = RemoteController(
        server=server,
        tunnel_factory=fake_tunnel_factory(url=PUBLIC),
        url_timeout=2,
        now=lambda: clock[0],
    )

    async def go(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        modal = await open_panel(pilot)
        modal.query_one("#remote-on", Switch).toggle()
        await pilot.pause()
        assert app.remote.running
        modal.query_one("#remote-on", Switch).toggle()
        await pilot.pause()
        assert server.stopping.wait(5) and server.running, "the switch waited for it to stop"
        assert shown(modal.query_one("#remote-state", Static)) == "off"
        assert shown(modal.query_one("#remote-status", Static)) == remote_control.TURNING_OFF
        server.release.set()
        assert app.remote.wait_until_off(5)
        modal.repaint()
        assert shown(modal.query_one("#remote-status", Static)) == ""

        server.stopping.clear()
        server.release.clear()
        modal.query_one("#remote-on", Switch).toggle()
        await pilot.pause()
        assert app.remote.running
        clock[0] += timedelta(minutes=61)
        app._remote_auto_off()  # the app's 30 s timer
        assert server.stopping.wait(5) and server.running, "the timer waited for it to stop"
        assert not app.remote.running
        server.release.set()

    drive_controller(go, controller)
    assert controller.wait_until_off(5) and not server.running


def test_quitting_leaves_remote_stopping_and_run_ui_waits_for_it_with_the_terminal_back(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Quit stopped Remote in ``on_unmount``, with the screen frozen on its last frame for as
    long as uvicorn and ngrok took. The app leaves at once and ``run_ui`` waits for them
    once the terminal is back, saying so when they take a while: no ngrok outlives the TUI."""
    server = SlowServer(patience=10.0)
    controller = RemoteController(
        server=server, tunnel_factory=fake_tunnel_factory(url=PUBLIC), url_timeout=2
    )

    async def go(pilot: Pilot[None]) -> None:
        controller.turn_on()
        assert controller.running

    _result, leaving = drive_controller(go, controller)
    assert leaving < 5.0, f"quitting waited {leaving:.1f} s for Remote to stop"
    assert server.stopping.wait(5) and controller.wait_until_off(0) is False
    server.release.set()
    assert controller.wait_until_off(5) and not server.running

    slow = SlowServer(patience=3.0)
    leaving_remote = RemoteController(server=slow, tunnel_factory=fake_tunnel_factory(url=PUBLIC))
    leaving_remote.turn_on()

    class Quit:
        def __init__(self, **options: object) -> None:
            self.unsaved: list[str] = []
            self.remote = leaving_remote

        def run(self) -> None:
            self.remote.shutdown_for_exit(wait=False)  # what on_unmount does
            threading.Timer(1.0, slow.release.set).start()

    monkeypatch.setattr(app_mod, "FleetApp", Quit)
    app_mod.run_ui()
    assert slow.release.is_set() and not slow.running, "run_ui returned before Remote stopped"
    assert "stopping Remote (its server and ngrok)…" in capsys.readouterr().err


@contextlib.contextmanager
def at_their_default(signums: Sequence[signal.Signals]) -> Iterator[None]:
    """``signums`` at their default for a while, as a fleet UI started from a terminal has
    them: a suite run under nohup has SIGHUP ignored, which ``ngrok_ends_with`` leaves as it
    is, and every assertion about the hangup then failed."""
    inherited = {signum: signal.getsignal(signum) for signum in signums}
    for signum in signums:
        signal.signal(signum, signal.SIG_DFL)
    try:
        yield
    finally:
        for signum, handler in inherited.items():
            if handler is not None:
                signal.signal(signum, handler)


def test_run_ui_ends_its_ngrok_on_a_hangup_or_a_sigterm_and_puts_the_signals_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ngrok runs in a process group of its own, which a closed terminal's hangup does not
    reach (``ngrok_ends_with``): for as long as the fleet UI runs, its ending signals end
    its ngrok first, and once it is gone they are what they were."""
    if sys.platform == "win32":  # an `if`, not a skipif: mypy's platform check reads only this
        pytest.skip("POSIX signals")
    ending = [signal.SIGHUP, signal.SIGTERM]
    ended: list[str] = []
    during: dict[int, object] = {}

    class Quit:
        def __init__(self, **options: object) -> None:
            self.unsaved: list[str] = []
            self.remote = SimpleNamespace(wait_until_off=lambda timeout=None: True)

        def run(self) -> None:
            during.update({signum: signal.getsignal(signum) for signum in ending})

    monkeypatch.setattr(app_mod, "FleetApp", Quit)
    monkeypatch.setattr(remote_control, "end_every_tunnel_now", lambda: ended.append("ngrok"))
    monkeypatch.setattr(remote_server, "remote_wait_for_writes", lambda: None)
    with at_their_default(ending):
        app_mod.run_ui()
        assert all(callable(handler) for handler in during.values()), during
        assert all(signal.getsignal(signum) is signal.SIG_DFL for signum in ending)
    hangup = during[signal.SIGHUP]
    assert callable(hangup)
    with pytest.MonkeyPatch.context() as dying:
        dying.setattr(os, "kill", lambda pid, signum: ended.append(f"killed by {signum}"))
        dying.setattr(signal, "signal", lambda signum, handler: None)
        hangup(signal.SIGHUP, None)
    assert ended == ["ngrok", f"killed by {signal.SIGHUP}"], "ngrok first, then the UI as ever"


# --- what Remote says reaches the human with the panel closed (sweep of #243) ------------------


def test_a_remote_that_does_not_come_back_at_start_is_toasted() -> None:
    """``on_mount`` brought a Remote that was on back, and the reason it could not was only
    in the R panel: a port taken by something else said nothing on the main screen, and the
    human found out from the phone, away from the desk (sweep of #243)."""
    update_state("remote_enabled", True)
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        controller = RemoteController(
            server=remote_server, tunnel_factory=missing_ngrok, port=taken.getsockname()[1]
        )

        async def go(pilot: Pilot[None]) -> None:
            app = pilot.app
            assert isinstance(app, FleetApp)
            await pilot.pause()
            assert not app.remote.running
            said = [note for note in toasts(app) if note.startswith("Remote could not start — ")]
            assert said and "is the port in use?" in said[0]

        drive_controller(go, controller)


def test_a_tunnel_that_does_not_come_up_after_a_restore_is_toasted_from_its_thread() -> None:
    """The wait for ngrok's URL ends on a thread of its own, and its sentence reached only
    the status line of a panel nobody had open."""
    update_state("remote_enabled", True)
    controller = RemoteController(
        server=FakeServer(), tunnel_factory=fake_tunnel_factory(url=None), url_timeout=0.2
    )

    async def go(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        await written(pilot)
        assert app.remote.running and app.remote._waiter is not None
        app.remote._waiter.join(5)
        await pilot.pause()
        assert (
            "Remote is on, but phones cannot reach it — ngrok did not announce a tunnel in time"
            in toasts(app)
        )

    drive_controller(go, controller)


def test_auto_off_is_toasted_unless_the_panel_says_it() -> None:
    clock = [datetime.now(UTC)]
    controller = RemoteController(
        server=FakeServer(), tunnel_factory=fake_tunnel_factory(url=PUBLIC), now=lambda: clock[0]
    )
    ran_out = "Remote turned off — the auto-off timer ran out"

    async def go(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        app.remote.turn_on()
        clock[0] += timedelta(minutes=61)
        app._remote_auto_off()  # the app's 30 s timer, the panel closed
        await pilot.pause()
        assert ran_out in toasts(app)
        app.clear_notifications()

        modal = await open_panel(pilot)
        modal.query_one("#remote-on", Switch).toggle()
        await pilot.pause()
        assert app.remote.running
        clock[0] += timedelta(minutes=61)
        app._remote_auto_off()  # with the panel open, its status line says it
        await pilot.pause()
        assert ran_out not in toasts(app)
        modal.repaint()
        assert shown(modal.query_one("#remote-status", Static)) == ran_out

    drive_controller(go, controller)
