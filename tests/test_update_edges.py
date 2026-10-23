"""Edges of asq's release check and one-click update that the PR's own tests leave open.

A day of hourly asks while asq stays open, the opt-out set while asq runs, the hourly ask
being scheduled at all, and an agent that exits while the update dialog is open (before the
press). Every way out of the process is closed as in tests/test_release_check.py and
tests/test_ui_update_row.py, whose fakes these reuse.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot

from aisquare.cli.ui import app as ui_app
from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.sidebar import UpdateRow
from aisquare.models import FleetAgent, FleetAgentStatus
from aisquare.services import fleet as fleet_service
from aisquare.services import settings as settings_service
from aisquare.services import updates
from aisquare.services.install_route import LatestRelease
from tests.installer_seams import no_real_installer  # noqa: F401 — autouse, applied by import
from tests.test_lifecycle_upgrade import Machine, Tool, machine, tool  # noqa: F401
from tests.test_release_check import NOW, _answered_at, running  # noqa: F401
from tests.test_ui_shell import Script, no_real_tmux, script, status  # noqa: F401
from tests.test_ui_update_row import (
    OFFER,
    _drive,
    _open_by_keyboard,
    _toasts,
    _two_alive,
    fresh,  # noqa: F401 — autouse: the dialog's ask of PyPI as it opens, stood in for
    planned,  # noqa: F401
)
from tests.ui_workers import settle_page

# --- the check over a day asq stays open ------------------------------------------------------


def test_a_day_of_hourly_asks_looks_up_again_only_at_the_24h_tick(
    running: Tool,  # noqa: F811
    machine: Machine,  # noqa: F811
) -> None:
    """asq open for a day: the start's lookup, 23 hourly asks that ask nothing, then the 24h
    tick's lookup, whose newer release is announced once."""
    first = updates.check_for_release(NOW)
    hourly = [updates.check_for_release(NOW + timedelta(hours=h)) for h in range(1, 24)]

    assert machine.lookups == 1
    assert first is not None and first.announce
    assert all(o is not None and o.version == "0.9.1" and not o.announce for o in hourly)

    machine.latest = LatestRelease("0.9.2")
    tick = updates.check_for_release(NOW + timedelta(hours=24))
    after = updates.check_for_release(NOW + timedelta(hours=25))

    assert machine.lookups == 2, "the 24h tick asks PyPI, the next hour does not"
    assert tick is not None and tick.version == "0.9.2" and tick.announce
    assert after is not None and after.version == "0.9.2" and not after.announce


def test_the_opt_out_set_while_asq_runs_withdraws_the_offer_without_a_lookup(
    running: Tool,  # noqa: F811
    machine: Machine,  # noqa: F811
) -> None:
    """An answer already kept (the row on screen), then ``config set updates.check false``:
    the next hourly ask offers nothing and asks nothing, even once a day has passed."""
    _answered_at(NOW - timedelta(hours=1), latest="0.9.1")
    assert updates.check_for_release(NOW) is not None, "control: the row is up"

    settings_service.set_value("updates.check", "false")

    assert updates.check_for_release(NOW + timedelta(hours=1)) is None
    assert updates.check_for_release(NOW + timedelta(days=2)) is None
    assert machine.lookups == 0


# --- the hourly ask is scheduled --------------------------------------------------------------


def test_the_app_asks_again_on_its_own_schedule_and_toasts_a_newer_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ask repeats by itself (the app's housekeeping tick, which has the hourly ask in it),
    with no one calling ``check_release``: the release the 24h tick finds replaces the row's
    and is toasted."""
    monkeypatch.setattr(updates, "ASK_EVERY_SECONDS", 0.05)
    monkeypatch.setattr(ui_app, "HOUSEKEEPING_SECONDS", 0.05)
    newer = updates.Offer(version="0.9.2", current="0.9.0", runnable=True, announce=True)
    asked: list[int] = []

    def release_check() -> updates.Offer | None:
        asked.append(1)
        return OFFER if len(asked) == 1 else newer

    async def body(pilot: Pilot[None], app: FleetApp) -> tuple[str | None, list[str]]:
        for _ in range(40):
            await pilot.pause(0.05)
            if len(asked) >= 3:
                break
        await settle_page(app)
        return app.query_one(UpdateRow).version, await _toasts(app)

    version, toasts = _drive(body, release_check=release_check)

    assert len(asked) >= 2, "the hourly ask runs on its own"
    assert version == "0.9.2"
    assert any("aisquare 0.9.2 is out" in toast for toast in toasts)


# --- the press: only the agents alive AT the press --------------------------------------------


def test_an_agent_that_exits_while_the_dialog_is_open_is_not_recorded(
    tmp_path: Path,
    script: Script,  # noqa: F811
    planned: list[dict[str, Any]],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reviewer-1 exits after the dialog opened and before *Restart & update*: it was not live
    at the press, so the reopen must not restart it. The frame on screen still shows it alive
    (the sidebar re-reads the fleet every ``refresh_seconds``; the real app's is 2 s)."""
    _two_alive(tmp_path, script)

    def exited_now(agent: FleetAgent) -> FleetAgentStatus:
        state = "exited" if agent.label in {"reviewer-1", "old-1"} else "working"
        return FleetAgentStatus(agent=agent, state=state)

    async def body(pilot: Pilot[None], app: FleetApp) -> None:
        await _open_by_keyboard(pilot, app)
        # reviewer-1's process ends now: every fresh read of the fleet says so.
        script["prj_a"] = [
            status("prj_a", "coder-1", "coder", "working"),
            status("prj_a", "reviewer-1", "reviewer", "exited", exit_status=0),
            status("prj_a", "old-1", "coder", "exited", exit_status=0),
        ]
        monkeypatch.setattr(fleet_service, "status_of", exited_now)
        await pilot.click("#update-confirm")
        await settle_page(app)

    _drive(body)

    press = updates.take_press()
    assert press is not None
    assert [k.label for k in press.agents] == ["coder-1"], (
        "an agent that had exited before the press is recorded, and the reopen restarts it"
    )
