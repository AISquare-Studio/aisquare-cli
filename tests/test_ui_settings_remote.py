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
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, Checkbox, Collapsible, DataTable, Select, Static, Switch
from textual.widgets._collapsible import CollapsibleTitle

from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.sidebar import SettingsRow
from aisquare.cli.ui.views import settings_remote
from aisquare.cli.ui.views.remote import LOCAL_ONLY, RemotePanel, qr_text
from aisquare.cli.ui.views.settings_remote import (
    AUTHTOKEN_COMMAND,
    RemoteChip,
    RemoteSection,
    has_authtoken,
    ngrok_install_command,
)
from aisquare.services import remote_server
from aisquare.services.ngrok_tunnel import NgrokTunnel, build_public_url
from aisquare.services.remote_server import remote_install_hint
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
    """Everything Remote needs: the extra (this install has it), ngrok and its authtoken."""
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))  # restored after the test
    path_with(tmp_path, "ngrok")
    monkeypatch.setenv("NGROK_AUTHTOKEN", "a-token")


@pytest.fixture
def nothing_of_ngrok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No ngrok on PATH and no authtoken anywhere: the extra alone is there."""
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    path_with(tmp_path)
    monkeypatch.delenv("NGROK_AUTHTOKEN", raising=False)


def late(tunnels: list[FakeTunnel]) -> Any:
    """A tunnel factory whose ngrok announces nothing until the test says so."""

    def make(port: int) -> NgrokTunnel:
        tunnels.append(FakeTunnel(port, url=None, failure=None))
        return tunnels[-1]

    return make


def announce(tunnel: FakeTunnel, url: str = PUBLIC) -> None:
    tunnel.handle_line(json.dumps({"lvl": "info", "msg": "started tunnel", "url": url}))


async def opened(pilot: Pilot[None]) -> RemoteSection:
    """Open Settings as a click on *⚙ Settings* does; its Remote section."""
    app = pilot.app
    assert isinstance(app, FleetApp)
    await pilot.click(app.query_one(SettingsRow))
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


# --- Off: the checklist ---------------------------------------------------------------------


def test_off_with_ngrok_and_its_token_missing_shows_the_checklist_with_each_command(
    nothing_of_ngrok: None,
) -> None:
    """What is missing is a line each, its command beside a Copy that copies it; what is
    there (the extra) is no line. With the extra, *Turn on anyway* stays: Remote serves this
    machine without ngrok, and a token kept where the check does not look still works."""

    async def go(pilot: Pilot[None]) -> tuple[dict[str, bool], str, str, str, list[str]]:
        app = pilot.app
        assert isinstance(app, FleetApp)
        section = await opened(pilot)
        lines = {
            key: displayed(section, f"#settings-remote-need-{key}")
            for key in ("extra", "ngrok", "authtoken")
        }
        copied = copies(app)
        section.query_one("#settings-remote-need-authtoken-copy", Button).press()
        await pilot.pause()
        return (
            lines,
            shown(section.query_one("#settings-remote-need-ngrok-text", Static)),
            shown(section.query_one("#settings-remote-need-authtoken-text", Static)),
            str(section.query_one("#settings-remote-turn-on", Button).label),
            copied,
        )

    lines, ngrok, token, turn_on, copied = drive(go, tunnel=missing_ngrok)
    assert lines == {"extra": False, "ngrok": True, "authtoken": True}
    assert ngrok_install_command() in ngrok
    assert AUTHTOKEN_COMMAND in token
    assert turn_on == "Turn on anyway"
    assert copied == [AUTHTOKEN_COMMAND]


def test_off_without_the_remote_extra_shows_its_install_command_and_no_turn_on(
    ready: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the extra nothing can start: the checklist's line is the command that adds it
    to the install that runs (``remote_install_hint``), and it stands in for *Turn on*."""
    monkeypatch.setitem(sys.modules, "websockets", None)  # find_spec: not installed

    async def go(pilot: Pilot[None]) -> tuple[bool, str, bool]:
        section = await opened(pilot)
        return (
            displayed(section, "#settings-remote-need-extra"),
            shown(section.query_one("#settings-remote-need-extra-text", Static)),
            displayed(section, "#settings-remote-turn-on"),
        )

    line, text, turn_on = drive(go, tunnel=missing_ngrok)
    assert line and remote_install_hint() in text
    assert not turn_on


def test_off_with_everything_there_offers_turn_on_and_no_checklist(ready: None) -> None:
    async def go(pilot: Pilot[None]) -> tuple[bool, str]:
        section = await opened(pilot)
        button = section.query_one("#settings-remote-turn-on", Button)
        return displayed(section, "#settings-remote-checklist"), str(button.label)

    assert drive(go, tunnel=missing_ngrok) == (False, "Turn on remote control")


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
        section.tick()
        section.tick()  # a second tick copies nothing more
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
        chip.tick()
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


def test_the_ngrok_install_command_is_this_platforms() -> None:
    assert settings_remote.ngrok_install_command("darwin") == "brew install ngrok"
    assert "winget" in settings_remote.ngrok_install_command("win32")
    assert "snap install ngrok" in settings_remote.ngrok_install_command("linux")
