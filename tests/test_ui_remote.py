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
import json
import shutil
import socket
from collections.abc import Awaitable, Callable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, Select, Static, Switch

from aisquare.cli.ui.app import FleetApp, HelpScreen
from aisquare.cli.ui.remote_control import READ_ONLY_REASON, RemoteController
from aisquare.cli.ui.views.remote import RemotePanel, qr_text
from aisquare.core import paths
from aisquare.core import tmux as tmux_core
from aisquare.core.state_file import update_state
from aisquare.core.tmux import Completed
from aisquare.models import FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import remote_page, remote_server
from aisquare.services.ngrok_tunnel import INSTALL_HINT, NgrokTunnel, build_public_url
from aisquare.services.remote_server import UNLOCK_GLOBAL_FAILURES, Runtime, UnlockBudget
from tests.test_remote_control import FakeTunnel, fake_tunnel_factory

T = TypeVar("T")
SIZE = (140, 40)
PUBLIC = "https://abcd-12.ngrok-free.app"
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
            return await fn(pilot)

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


def panel(pilot: Pilot[None]) -> RemotePanel:
    screen = pilot.app.screen
    assert isinstance(screen, RemotePanel), f"the top screen is a {type(screen).__name__}"
    return screen


async def open_panel(pilot: Pilot[None]) -> RemotePanel:
    await pilot.press("R")
    await pilot.pause()
    return panel(pilot)


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
        await pilot.pause()

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
        await pilot.pause()
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
        await pilot.pause()
        left = devices()
        assert len(left) == 1 and left[0]["ua"] == "Firefox"  # the cursor was on the first row
        assert table.row_count == 1  # type: ignore[attr-defined]

    drive(go, tunnel=missing_ngrok)


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
        assert not app.remote.running
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
    and the exception ended the fleet UI while Remote kept serving (r2 review of #243)."""

    async def go(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        modal = await open_panel(pilot)
        modal.query_one("#remote-on", Switch).toggle()
        await pilot.pause()
        runtime = remote_server.runtime()
        assert runtime.unlock_device(runtime.password, "iPhone Safari") is not None
        modal.repaint()
        await pilot.pause()
        refuse_remote_json(monkeypatch)

        def status() -> str:
            return shown(modal.query_one("#remote-status", Static))

        modal.query_one("#remote-auto-off", Select).value = 30
        await pilot.pause()
        assert status().startswith("auto-off could not be saved to remote.json — [Errno 13]")
        modal.query_one("#remote-revoke", Button).press()
        await pilot.pause()
        assert "could not be revoked in remote.json" in status()
        modal.query_one("#remote-regen", Button).press()
        await pilot.pause()
        assert status().startswith("the new password could not be saved to remote.json")
        assert app.screen is modal and app.remote.running

        modal.query_one("#remote-on", Switch).toggle()  # off still goes off
        await pilot.pause()
        assert not app.remote.running
        assert remote_server.remote_server_status()["running"] is False
        assert "devices could not be revoked" in status()

        modal.query_one("#remote-on", Switch).toggle()  # and on does not stay on without it
        await pilot.pause()
        assert app.screen is modal and not app.remote.running
        assert modal.query_one("#remote-on", Switch).value is False
        assert status().startswith("Remote could not start — remote.json could not be written")
        assert remote_server.remote_server_status()["running"] is False

    drive(go, tunnel=missing_ngrok)


def test_the_modal_shows_failed_unlocks_and_a_deadline_a_phone_extended() -> None:
    async def go(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert isinstance(app, FleetApp)
        modal = await open_panel(pilot)
        modal.query_one("#remote-on", Switch).toggle()
        await pilot.pause()
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
