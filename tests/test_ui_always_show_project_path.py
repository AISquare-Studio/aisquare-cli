"""*Always show project directory* (App settings ▸ User preferences, ``[ui]
always_show_project_path``): on, every card in the sidebar shows its directory under its
name; off, the default, only two projects sharing a name do.

Driven through the whole app: the preference is set on the form, and what is asserted is
the cards' dim line, right after Save and after a tick. The app holds the value — read at
the start, then told by App settings — so the two-second tick never reads it.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, Static, Switch

from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.sidebar import (
    AppSettingsSelected,
    project_name,
    project_subtitle,
    short_path,
)
from aisquare.models import ProjectInfo
from aisquare.services import settings as settings_service
from tests.test_ui_shell import card_for, drive, fleet_app, refreshed, seed, settle, shown
from tests.test_ui_shell import no_real_tmux as _no_real_tmux
from tests.test_ui_shell import script as _script

#: Re-exported so pytest collects them here (the guard is autouse there, and here).
no_real_tmux = _no_real_tmux
script = _script

TWINS_AND_ONE = (
    ("prj_w", "work/api", "amber-otter"),
    ("prj_o", "oss/api", "ruby-fox"),
    ("prj_u", "unique", None),
)
"""Two projects named ``api``, one named ``unique``."""


def _directory(tmp_path: Path, rel: str) -> str:
    """The line a seeded project's card shows: :func:`short_path` of its root, with the
    platform's own separators — the sidebar's helper, so no ``/`` is assumed here."""
    return short_path(tmp_path / rel)


def _subtitles(app: FleetApp) -> dict[str, str | None]:
    """Each card's directory line as it is shown: its text, or ``None`` when hidden."""
    lines: dict[str, str | None] = {}
    for pid in ("prj_w", "prj_o", "prj_u"):
        line = card_for(app, pid).query_one(".card-subtitle", Static)
        lines[pid] = shown(line) if line.display else None
    return lines


async def _open_app_settings(app: FleetApp) -> None:
    app.post_message(AppSettingsSelected())
    await settle(app)


async def _save_preference(app: FleetApp, on: bool) -> None:
    await _open_app_settings(app)
    app.query_one("#ui-always-show-path", Switch).value = on
    app.query_one("#save-settings", Button).press()
    await settle(app)


def test_the_subtitle_rule_is_twins_only_unless_always() -> None:
    projects = [
        ProjectInfo(id="prj_w", root=Path("/w", "work", "api")),
        ProjectInfo(id="prj_o", root=Path("/w", "oss", "api")),
        ProjectInfo(id="prj_u", root=Path("/w", "unique")),
    ]
    names = Counter(project_name(project) for project in projects)
    off = [project_subtitle(project, names, always=False) for project in projects]
    on = [project_subtitle(project, names, always=True) for project in projects]
    # The sidebar's own helper: native separators, ``\w\work\api`` on Windows.
    work, oss, unique = (short_path(project.root) for project in projects)
    assert off == [work, oss, None]
    assert on == [work, oss, unique]


def test_off_by_default_only_twins_show_their_directory(tmp_path: Path) -> None:
    seed(tmp_path, *TWINS_AND_ONE)

    async def go(pilot: Pilot[None]) -> tuple[bool, dict[str, str | None]]:
        app = fleet_app(pilot)
        return app.always_show_project_path, _subtitles(app)

    always, lines = drive(go)
    assert always is False
    assert lines == {
        "prj_w": _directory(tmp_path, "work/api"),
        "prj_o": _directory(tmp_path, "oss/api"),
        "prj_u": None,
    }


def test_saving_it_on_shows_every_directory_at_once_and_off_brings_the_twins_rule_back(
    tmp_path: Path,
) -> None:
    seed(tmp_path, *TWINS_AND_ONE)

    async def go(pilot: Pilot[None]) -> dict[str, dict[str, str | None]]:
        app = fleet_app(pilot)
        seen = {"before": _subtitles(app)}
        await _save_preference(app, True)
        seen["after save"] = _subtitles(app)  # no tick yet: the app refreshes hourly here
        await refreshed(app)
        seen["after a tick"] = _subtitles(app)
        await _save_preference(app, False)
        seen["off again"] = _subtitles(app)
        await refreshed(app)
        seen["off, after a tick"] = _subtitles(app)
        return seen

    seen = drive(go)
    assert seen["before"]["prj_u"] is None
    unique = seen["after save"]["prj_u"]
    assert unique == _directory(tmp_path, "unique"), seen
    assert seen["after a tick"] == seen["after save"], "the frame keeps what was saved"
    for state in ("off again", "off, after a tick"):
        lines = seen[state]
        assert lines["prj_u"] is None, (state, lines)
        assert lines["prj_w"] is not None and lines["prj_o"] is not None, (state, lines)
    assert settings_service.ui_settings().always_show_project_path is False


def test_it_is_read_at_the_start_and_reset_all_turns_it_off(tmp_path: Path) -> None:
    seed(tmp_path, *TWINS_AND_ONE)
    settings_service.set_value("ui.always_show_project_path", "true")

    async def go(pilot: Pilot[None]) -> tuple[str | None, str | None]:
        app = fleet_app(pilot)
        at_start = _subtitles(app)["prj_u"]
        await _open_app_settings(app)
        app.query_one("#reset-settings", Button).press()
        await settle(app)
        app.screen.query_one("#reset-confirm", Button).press()
        await settle(app)
        return at_start, _subtitles(app)["prj_u"]

    at_start, after_reset = drive(go)
    assert at_start == _directory(tmp_path, "unique")
    assert after_reset is None


def test_the_refresh_tick_never_reads_the_preference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sidebar repaints every two seconds; the preference changes only on App settings,
    so the app holds it rather than reading ``config.toml`` for it each frame. A ``config
    set`` from a shell shows at the next start, or after App settings' Reload."""
    seed(tmp_path, *TWINS_AND_ONE)
    reads: list[int] = []
    real = settings_service.ui_settings

    def counted() -> object:
        reads.append(1)
        return real()

    monkeypatch.setattr(settings_service, "ui_settings", counted)

    async def go(pilot: Pilot[None]) -> tuple[int, int, str | None, str | None]:
        app = fleet_app(pilot)
        at_start = len(reads)
        settings_service.set_value("ui.always_show_project_path", "true")  # a shell
        for _ in range(3):
            await refreshed(app)
        ticked = _subtitles(app)["prj_u"]
        await _open_app_settings(app)
        app.query_one("#reload-settings", Button).press()
        await settle(app)
        return at_start, len(reads), ticked, _subtitles(app)["prj_u"]

    at_start, after_ticks, ticked, reloaded = drive(go)
    assert at_start == 1 and after_ticks == 1, "read once, at the start"
    assert ticked is None, "the ticks painted with the value the app holds"
    assert reloaded == _directory(tmp_path, "unique"), "Reload hands it over"
