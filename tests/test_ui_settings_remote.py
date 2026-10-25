"""Settings' *Remote control* (#33): the checklist, on and off, the link it copies, Advanced.

On a fresh install the owner could not find Remote: it lived in the ``R`` modal alone. The
section is the same Remote, over the app's one controller. Driven as ``test_ui_remote``
drives the modal: the REAL ``services.remote_server`` on a free port, a scripted tunnel,
and every claim read from what a widget shows.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import (
    Button,
    Checkbox,
    Collapsible,
    DataTable,
    Input,
    Select,
    Static,
    Switch,
)
from textual.widgets._collapsible import CollapsibleTitle

from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.sidebar import SettingsRow
from aisquare.cli.ui.views import settings_remote
from aisquare.cli.ui.views.remote import LOCAL_ONLY, RemotePanel, qr_text
from aisquare.cli.ui.views.settings_remote import (
    AUTHTOKEN_FROM,
    ENV_TOKEN,
    NGROK_MISSING,
    REPLACE_TITLE,
    TOKEN_TITLE,
    RemoteChip,
    RemoteSection,
    has_authtoken,
)
from aisquare.services import remote_server
from aisquare.services.ngrok_sdk import (
    BAD_TOKEN,
    REFUSED_TOKEN,
    bundled_ngrok,
    save_token,
    saved_token,
)
from aisquare.services.ngrok_tunnel import AUTHTOKEN_HINT, NgrokTunnel, build_public_url
from tests.fake_clipboard import NEEDS_POSIX
from tests.fake_ngrok_sdk import Script, fake_sdk
from tests.test_remote_control import FakeTunnel
from tests.test_ui_remote import (  # noqa: F401 — the autouse fixtures, applied by import
    PUBLIC,
    drive,
    fresh_remote_server,
    installed_page,
    missing_ngrok,
    no_agents,
    no_real_tmux,
    shown,
    written,
)
from tests.toast_log import record
from tests.ui_workers import settle_page

ON_IDS = ("settings-remote-link", "settings-remote-qr", "settings-remote-password")


def path_with(tmp_path: Path, *tools: str) -> None:
    """PATH holding only stand-ins for ``tools``: what ``shutil.which`` finds, and all."""
    folder = tmp_path / "path"
    folder.mkdir(exist_ok=True)
    for tool in tools:
        stand_in = folder / (f"{tool}.bat" if sys.platform == "win32" else tool)
        stand_in.write_text("@echo off\n" if sys.platform == "win32" else "#!/bin/sh\n")
        stand_in.chmod(0o755)
    os.environ["PATH"] = str(folder)


@pytest.fixture
def ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Everything Remote needs from the owner: an ngrok to start and its token."""
    own_home(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))  # restored after the test
    path_with(tmp_path, "ngrok")
    monkeypatch.setenv("NGROK_AUTHTOKEN", "a-token")


def late(tunnels: list[FakeTunnel]) -> Any:
    """A tunnel factory whose ngrok announces nothing until the test says so."""

    def make(port: int) -> NgrokTunnel:
        tunnels.append(FakeTunnel(port, url=None, failure=None))
        return tunnels[-1]

    return make


def announce(tunnel: FakeTunnel, url: str = PUBLIC) -> None:
    tunnel.handle_line(json.dumps({"lvl": "info", "msg": "started tunnel", "url": url}))


async def opened(pilot: Pilot[None]) -> RemoteSection:
    """Open Settings as a click on *⚙ Settings* does, then its *Remote control* link; App
    settings' Remote tab, where it lands (#41)."""
    app = pilot.app
    assert isinstance(app, FleetApp)
    await pilot.click(app.query_one(SettingsRow))
    await settle_page(app)
    await pilot.click("#hub-remote")
    await settle_page(app)
    return app.query_one(RemoteSection)


def displayed(section: RemoteSection, selector: str) -> bool:
    widget = section.query_one(selector)
    return all(node.display for node in (widget, *widget.ancestors) if hasattr(node, "display"))


def copies(app: FleetApp) -> list[str]:
    """Every text the app is asked to copy from now on, each still copied."""
    copied: list[str] = []
    copy = app.copy_to_clipboard

    def recorded(text: str, **options: Any) -> None:
        copied.append(text)
        copy(text, **options)

    app.copy_to_clipboard = recorded  # type: ignore[method-assign]
    return copied


# --- Off: the token, and nothing to install --------------------------------------------------

NOT_ON_THE_NORMAL_PATH = ("pip install", "uv ", "[remote]", "not installed", "ngrok config", "brew")
"""What Settings' Remote never tells the owner to do (the owner, 2026-10-23, #34)."""


def said_in(section: RemoteSection) -> str:
    """Every sentence the section shows now, one per line."""
    return "\n".join(
        shown(static)
        for static in section.query(Static).results(Static)
        if not isinstance(static, Button) and all(node.display for node in _chain(static))
    )


def _chain(widget: Any) -> list[Any]:
    return [widget, *(node for node in widget.ancestors if hasattr(node, "display"))]


def own_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A home of the test's own, where ngrok's config is looked for and written: the suite
    isolates the aisquare home, not ``~``, which holds the developer's real ngrok config."""
    home = tmp_path / "user"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("NGROK_AUTHTOKEN", raising=False)


def bundled_ngrok_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, refuse: str | None = None
) -> Script:
    """The bundled ngrok (#34), answering as ngrok's service would: every sign-in taken, or,
    with ``refuse``, refused with that code; in the test's own home, with no token anywhere."""
    own_home(tmp_path, monkeypatch)
    script = Script(url=PUBLIC, refuse=refuse)
    monkeypatch.setitem(sys.modules, "ngrok", fake_sdk(script))
    return script


async def paste(pilot: Pilot[None], section: RemoteSection, token: str) -> None:
    """Paste ``token`` into the slot and Save, and let the save answer."""
    section.query_one("#settings-remote-token-field", Input).value = token
    section.query_one("#settings-remote-token-save", Button).press()
    await pilot.pause()
    await settle_page(pilot.app)
    await pilot.pause()


@NEEDS_POSIX
def test_off_with_no_ngrok_token_shows_only_the_token_field_and_its_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No token yet: *Paste your ngrok token*, where a free one comes from, and *Save*, in the
    place of *Turn on*; nothing to install and no command to run."""
    bundled_ngrok_with(tmp_path, monkeypatch)

    async def go(pilot: Pilot[None]) -> tuple[bool, bool, bool, str]:
        section = await opened(pilot)
        return (
            displayed(section, "#settings-remote-token-field"),
            displayed(section, "#settings-remote-turn-on"),
            displayed(section, "#settings-remote-ngrok-missing"),
            said_in(section),
        )

    field, turn_on, missing, said = drive(go, tunnel=missing_ngrok)
    assert field and not turn_on and not missing
    assert TOKEN_TITLE in said and AUTHTOKEN_FROM in said, said
    assert not [hint for hint in NOT_ON_THE_NORMAL_PATH if hint in said], said


@NEEDS_POSIX
def test_a_saved_token_brings_turn_on_back_and_turn_on_reaches_the_tunnel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Save checks the token with ngrok and keeps it, owner-only; the slot goes, *Turn on* is
    there, and a press of it starts the bundled tunnel, signed in with that token (#34). The
    token is in no toast."""
    script = bundled_ngrok_with(tmp_path, monkeypatch)
    token = "2abcDEF_0123456789abcdefghijklmnopqrstuvwxyzAB"

    async def go(pilot: Pilot[None]) -> tuple[bool, bool, list[str]]:
        app = pilot.app
        assert isinstance(app, FleetApp)
        section = await opened(pilot)
        told = record(app)
        await paste(pilot, section, token)
        slot, turn_on = (
            displayed(section, "#settings-remote-token"),
            displayed(section, "#settings-remote-turn-on"),
        )
        section.query_one("#settings-remote-turn-on", Button).press()
        await written(pilot)
        await settle_page(app)
        return slot, turn_on, told

    slot, turn_on, told = drive(go, tunnel=bundled_ngrok)
    assert saved_token() == token
    assert not slot and turn_on
    assert "✓ ngrok token saved" in told and not [line for line in told if token in line]
    assert script.calls.tokens == [token, token], "checked on Save, then signed in on Turn on"
    assert len(script.calls.forwards) == 1, "Turn on started the tunnel"


def test_a_token_ngrok_refuses_is_said_beside_the_field_without_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundled_ngrok_with(tmp_path, monkeypatch, refuse="ERR_NGROK_105")
    token = "2abc-not-a-real-token"

    async def go(pilot: Pilot[None]) -> tuple[str, bool, str]:
        section = await opened(pilot)
        await paste(pilot, section, token)
        problem = section.query_one("#settings-remote-token-problem", Static)
        first = shown(problem) if problem.display else ""
        await paste(pilot, section, "two words")
        return first, displayed(section, "#settings-remote-token"), shown(problem)

    first, slot, spaced = drive(go, tunnel=missing_ngrok)
    assert first == REFUSED_TOKEN, first
    assert token not in first and slot
    assert saved_token() is None, "a token ngrok refuses is not kept"
    assert spaced == "that is not one token: it has spaces in it"


OLD_TOKEN = "2oldTOKEN_0123456789abcdefghijklmnopqrstuvwxy"
NEW_TOKEN = "2newTOKEN_9876543210zyxwvutsrqponmlkjihgfedcb"


async def until(pilot: Pilot[None], done: Callable[[], bool], *, what: str) -> None:
    """Pause until ``done()`` holds: ngrok's answer comes on a thread of its own."""
    for _ in range(500):
        if done():
            return
        await pilot.pause(0.01)
    raise AssertionError(f"never: {what}")


def slot_seen(section: RemoteSection) -> tuple[bool, str, bool]:
    """The token field shown, its title, and the ``NGROK_AUTHTOKEN`` line shown."""
    return (
        displayed(section, "#settings-remote-token-field"),
        shown(section.query_one("#settings-remote-token-title", Static)),
        displayed(section, "#settings-remote-token-env"),
    )


@NEEDS_POSIX
def test_a_saved_token_ngrok_refuses_can_be_replaced_on_and_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #274, F-1: ngrok refused the saved token, and Remote said to paste a new one
    in Settings → Remote, whose field showed only while there was no token at all: on, it
    was not there, and off, the line saying why was gone too. It shows on and off as *Replace
    your ngrok token*, and a new one saved and turned on signs in with it."""
    script = bundled_ngrok_with(tmp_path, monkeypatch)
    save_token(OLD_TOKEN)

    async def go(pilot: Pilot[None]) -> dict[str, Any]:
        app = pilot.app
        assert isinstance(app, FleetApp)
        section = await opened(pilot)
        seen: dict[str, Any] = {"before": slot_seen(section)[0]}
        script.refuse = "ERR_NGROK_107"  # reset on ngrok's dashboard since it was saved
        section.query_one("#settings-remote-turn-on", Button).press()
        await written(pilot)
        await until(pilot, lambda: app.remote.status_line() == BAD_TOKEN, what="ngrok refused")
        section.repaint()
        seen["on"] = slot_seen(section)
        section.query_one("#settings-remote-turn-off", Button).press()
        await pilot.pause()
        assert app.remote.wait_until_off(10)
        section.repaint()
        await settle_page(app)  # the look again, back at Off
        seen["off"] = (*slot_seen(section), displayed(section, "#settings-remote-turn-on"))
        script.refuse = None
        await paste(pilot, section, NEW_TOKEN)
        seen["saved"] = slot_seen(section)[0]
        section.query_one("#settings-remote-turn-on", Button).press()
        await written(pilot)
        await until(pilot, lambda: app.remote.public_origin is not None, what="signed in")
        return seen

    seen = drive(go, tunnel=bundled_ngrok)
    assert seen["before"] is False, "a token ngrok took asks for nothing"
    assert seen["on"] == (True, REPLACE_TITLE, False)
    assert seen["off"] == (True, REPLACE_TITLE, False, True), "Replace, and Turn on"
    assert seen["saved"] is False
    assert saved_token() == NEW_TOKEN
    assert script.calls.tokens == [OLD_TOKEN, NEW_TOKEN, NEW_TOKEN], "on, Save's check, on"


@NEEDS_POSIX
def test_a_remote_turned_on_with_no_token_shows_the_field_and_a_save_brings_ngrok_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Turned on with ``R`` and no token anywhere, Remote said to paste one in Settings →
    Remote, which showed its On view with no field. The field is there, and the token saved
    starts the tunnel at once, with no off and on (``RemoteController.token_saved``)."""
    script = bundled_ngrok_with(tmp_path, monkeypatch)

    async def go(pilot: Pilot[None]) -> tuple[str, bool, tuple[bool, str, bool], bool]:
        app = pilot.app
        assert isinstance(app, FleetApp)
        app.remote.turn_on(wait=False)  # what R's switch does
        await written(pilot)
        said = app.remote.status_line()
        section = await opened(pilot)
        on, slot = displayed(section, "#settings-remote-on"), slot_seen(section)
        await paste(pilot, section, NEW_TOKEN)
        await until(pilot, lambda: app.remote.public_origin is not None, what="ngrok up")
        section.repaint()
        return said, on, slot, slot_seen(section)[0]

    said, on, slot, after = drive(go, tunnel=bundled_ngrok)
    assert said == AUTHTOKEN_HINT
    assert on and slot == (True, TOKEN_TITLE, False)
    assert script.calls.tokens == [NEW_TOKEN, NEW_TOKEN], "Save's check, then the sign-in"
    assert not after


@NEEDS_POSIX
@pytest.mark.parametrize("environment", [False, True], ids=["saved", "NGROK_AUTHTOKEN"])
def test_change_ngrok_token_under_advanced_opens_the_field_and_says_when_the_env_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, environment: bool
) -> None:
    bundled_ngrok_with(tmp_path, monkeypatch)
    save_token(OLD_TOKEN)
    if environment:
        monkeypatch.setenv("NGROK_AUTHTOKEN", "2envTOKEN_0123456789")

    async def go(pilot: Pilot[None]) -> tuple[bool, tuple[bool, str, bool], str, bool]:
        section = await opened(pilot)
        before = slot_seen(section)[0]
        section.query_one("#settings-remote-advanced", Collapsible).collapsed = False
        await pilot.pause()
        section.query_one("#settings-remote-change-token", Button).press()
        await pilot.pause()
        opened_up = slot_seen(section)
        env_line = shown(section.query_one("#settings-remote-token-env", Static))
        await paste(pilot, section, NEW_TOKEN)
        return before, opened_up, env_line, slot_seen(section)[0]

    before, opened_up, env_line, after = drive(go, tunnel=missing_ngrok)
    assert not before
    assert opened_up == (True, REPLACE_TITLE, environment)
    assert env_line == ENV_TOKEN
    assert saved_token() == NEW_TOKEN and not after


def test_what_remote_needs_is_looked_at_off_the_ui_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #274, m6: Settings' first open, and each turn-off, imported ngrok's native
    library and parsed ngrok's configs on Textual's thread. A thread worker looks now."""
    bundled_ngrok_with(tmp_path, monkeypatch)
    looked_on: list[int] = []
    found, token = settings_remote.ngrok_found, settings_remote.has_authtoken

    def ngrok_found() -> bool:
        looked_on.append(threading.get_ident())
        return found()

    def has_authtoken() -> bool:
        looked_on.append(threading.get_ident())
        return token()

    monkeypatch.setattr(settings_remote, "ngrok_found", ngrok_found)
    monkeypatch.setattr(settings_remote, "has_authtoken", has_authtoken)

    async def go(pilot: Pilot[None]) -> tuple[int, bool]:
        section = await opened(pilot)
        return threading.get_ident(), displayed(section, "#settings-remote-token-field")

    ui, field = drive(go, tunnel=missing_ngrok)
    assert len(looked_on) == 2 and ui not in looked_on, (ui, looked_on)
    assert field, "its answer painted: no token, so the field"


@pytest.mark.parametrize("look", ["as it is", "slow"])
def test_a_missing_ngrok_is_one_line_with_a_fix_in_the_app_never_a_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, look: str
) -> None:
    """Only a damaged install, whose bundled ngrok cannot be imported, with none on PATH.

    ``slow``: the look again answers as late as Windows CI's runner answered it (#51), where
    the line was read before the answer came."""
    own_home(tmp_path, monkeypatch)
    monkeypatch.setitem(sys.modules, "ngrok", None)
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    path_with(tmp_path)
    if look == "slow":
        needs = settings_remote.remote_needs

        def slow() -> tuple[bool, bool, bool]:
            time.sleep(0.3)
            return needs()

        monkeypatch.setattr(settings_remote, "remote_needs", slow)
    monkeypatch.setenv("NGROK_AUTHTOKEN", "a-token")

    async def go(pilot: Pilot[None]) -> tuple[bool, str, bool]:
        section = await opened(pilot)
        missing = displayed(section, "#settings-remote-ngrok-missing")
        said = said_in(section)
        path_with(tmp_path, "ngrok")  # what the fix brings back
        section.query_one("#settings-remote-ngrok-fix", Button).press()
        # The look again is on a worker (#274 m6): one pause was enough here, not on
        # Windows CI's runner, which read the line before the answer came (#51).
        await until(
            pilot,
            lambda: not displayed(section, "#settings-remote-ngrok-missing"),
            what="the line gone once the fix brought ngrok back",
        )
        return missing, said, displayed(section, "#settings-remote-ngrok-missing")

    missing, said, after = drive(go, tunnel=missing_ngrok)
    assert missing and NGROK_MISSING in said
    assert not [hint for hint in NOT_ON_THE_NORMAL_PATH if hint in said], said
    assert not after


# --- On, and off again ----------------------------------------------------------------------


def test_turning_remote_on_shows_link_qr_password_and_copies_ngroks_link_once_it_arrives(
    ready: None,
) -> None:
    """The local link first, which a phone cannot open: no QR of it and nothing copied. Once
    ngrok announces its link, the next tick shows it with its QR and copies it, once, with
    a toast. The password is the one the phone unlocks with."""
    tunnels: list[FakeTunnel] = []

    async def go(pilot: Pilot[None]) -> dict[str, Any]:
        app = pilot.app
        assert isinstance(app, FleetApp)
        section = await opened(pilot)
        copied, told = copies(app), record(app)
        section.query_one("#settings-remote-turn-on", Button).press()
        await written(pilot)
        section.repaint()
        info = app.remote.info
        assert info is not None and tunnels, "Remote is on, its ngrok started"
        before = {
            "on": displayed(section, "#settings-remote-on"),
            "off": displayed(section, "#settings-remote-off"),
            "link": shown(section.query_one("#settings-remote-link", Static)),
            "qr": shown(section.query_one("#settings-remote-qr", Static)),
            "copied": list(copied),
        }
        announce(tunnels[0])
        section.remote_tick()
        section.remote_tick()  # a second tick copies nothing more
        await pilot.pause()
        return {
            "before": before,
            "local": info.url_local,
            "public": build_public_url(PUBLIC, info.token),
            "link": shown(section.query_one("#settings-remote-link", Static)),
            "qr": shown(section.query_one("#settings-remote-qr", Static)),
            "password": shown(section.query_one("#settings-remote-password", Static)),
            "copied": copied,
            "told": told,
        }

    seen = drive(go, tunnel=late(tunnels))
    before = seen["before"]
    assert before["on"] and not before["off"]
    assert before["link"] == f"{seen['local']}\n{LOCAL_ONLY}"
    assert before["qr"] == "" and before["copied"] == []
    assert seen["link"] == seen["public"]
    assert seen["qr"] == qr_text(seen["public"])
    assert seen["password"] == f"Password  {remote_server.runtime().password}"
    assert seen["copied"] == [seen["public"]]
    assert "Remote link copied" in seen["told"]


def test_turn_off_returns_the_section_to_off(ready: None) -> None:
    async def go(pilot: Pilot[None]) -> tuple[bool, bool, bool]:
        app = pilot.app
        assert isinstance(app, FleetApp)
        section = await opened(pilot)
        section.query_one("#settings-remote-turn-on", Button).press()
        await written(pilot)
        assert app.remote.running
        section.query_one("#settings-remote-turn-off", Button).press()
        await pilot.pause()
        assert app.remote.wait_until_off(10)
        return (
            app.remote.running,
            displayed(section, "#settings-remote-off"),
            displayed(section, "#settings-remote-on"),
        )

    assert drive(go, tunnel=late([])) == (False, True, False)


def test_the_section_and_the_r_panel_are_one_remote(ready: None) -> None:
    """One controller: the section is a view over the app's own, and ``R`` still opens the
    modal, which shows the Remote the section turned on, on the same link."""

    async def go(pilot: Pilot[None]) -> tuple[bool, bool, str, str]:
        app = pilot.app
        assert isinstance(app, FleetApp)
        section = await opened(pilot)
        section.query_one("#settings-remote-turn-on", Button).press()
        await written(pilot)
        section.repaint()
        link = shown(section.query_one("#settings-remote-link", Static))
        app.sidebar.focus()
        await pilot.press("R")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, RemotePanel)
        return (
            section.controller is app.remote is modal.controller,
            modal.query_one("#remote-on", Switch).value,
            link,
            shown(modal.query_one("#remote-link", Static)),
        )

    same, on, link, modal_link = drive(go, tunnel=late([]))
    assert same and on
    assert link == modal_link


# --- Advanced -------------------------------------------------------------------------------


def test_advanced_starts_collapsed_and_opens_onto_its_four_controls(ready: None) -> None:
    controls = (
        ("#settings-remote-regen", Button),
        ("#settings-remote-write", Checkbox),
        ("#settings-remote-auto-off", Select),
        ("#settings-remote-devices", DataTable),
        ("#settings-remote-revoke", Button),
    )

    async def go(pilot: Pilot[None]) -> tuple[bool, list[bool], bool, list[bool]]:
        section = await opened(pilot)
        advanced = section.query_one("#settings-remote-advanced", Collapsible)
        folded = [displayed(section, selector) for selector, _ in controls]
        was = advanced.collapsed
        title = advanced.query_one(CollapsibleTitle)
        title.scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click(title)
        await pilot.pause()
        opened_up = [
            displayed(section, selector) and section.query_one(selector, kind).region.area > 0
            for selector, kind in controls
        ]
        return was, folded, advanced.collapsed, opened_up

    was, folded, now, opened_up = drive(go, tunnel=missing_ngrok)
    assert was is True and not any(folded)
    assert now is False and all(opened_up), opened_up


# --- the sidebar's chip ---------------------------------------------------------------------


def test_the_chip_shows_while_remote_is_on_and_a_click_opens_the_r_panel(ready: None) -> None:
    async def go(pilot: Pilot[None]) -> tuple[bool, bool, str, bool]:
        app = pilot.app
        assert isinstance(app, FleetApp)
        chip = app.query_one(RemoteChip)
        off = chip.display
        section = await opened(pilot)
        section.query_one("#settings-remote-turn-on", Button).press()
        await written(pilot)
        chip.remote_tick()
        await pilot.pause()
        on, said = chip.display, shown(chip)
        await pilot.click(chip)
        await pilot.pause()
        return off, on, said, isinstance(app.screen, RemotePanel)

    off, on, said, panel = drive(go, tunnel=late([]))
    assert not off and on
    assert said == "📡 remote · 0 devices"
    assert panel


# --- the authtoken check --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "found"),
    [
        pytest.param('version: "3"\nagent:\n  authtoken: 2abc\n', True, id="v3, under agent"),
        pytest.param("authtoken: 2abc\nregion: eu\n", True, id="v2, top level"),
        pytest.param('version: "3"\nagent:\n  authtoken: ""\n', False, id="an empty one"),
        pytest.param('version: "3"\n# authtoken: 2abc\n', False, id="commented out"),
        pytest.param(None, False, id="no config"),
    ],
)
def test_the_authtoken_is_found_where_ngrok_config_add_authtoken_writes_it(
    tmp_path: Path, config: str | None, found: bool
) -> None:
    path = tmp_path / "ngrok.yml"
    if config is not None:
        path.write_text(config, encoding="utf-8")
    assert has_authtoken({}, [path]) is found


def test_the_authtoken_in_the_environment_signs_ngrok_in_too(tmp_path: Path) -> None:
    assert has_authtoken({"NGROK_AUTHTOKEN": "2abc"}, [tmp_path / "none.yml"])
    assert not has_authtoken({"NGROK_AUTHTOKEN": "  "}, [tmp_path / "none.yml"])
