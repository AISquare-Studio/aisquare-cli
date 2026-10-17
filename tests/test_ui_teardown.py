"""A timer's tick that lands while asq is being torn down does nothing, and raises nothing.

Textual's ``App._shutdown`` marks the app stopped, then prunes the screens; a timer already
due still runs. A two-second ``refresh_data`` landing there found the sidebar gone and
raised ``NoMatches('Sidebar')`` out of the quit (seen while fixing the status line, review
of #261). Here every interval callback asq runs is called at exactly that moment — after
the screens are pruned, before the app's own messages close — as a due tick would be.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.board import BoardPanel
from aisquare.cli.ui.sidebar import AccountsSelected, ProjectSelected
from aisquare.cli.ui.views.accounts import AccountsView
from aisquare.cli.ui.views.remote import RemotePanel
from aisquare.cli.ui.views.welcome import WelcomeView
from aisquare.models import HeadroomSummary
from aisquare.services import fleet as fleet_service
from tests.test_ui_accounts import _overview, _status
from tests.test_ui_shell import seed
from tests.ui_workers import settle_page

GOOD = HeadroomSummary(level="good", percent_left=80.0, usable=1)


def test_a_tick_that_lands_while_asq_is_torn_down_raises_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))
    monkeypatch.setattr(fleet_service, "list_agents", lambda project: [])
    callbacks: list[tuple[str, Callable[[], None]]] = []
    raised: list[str] = []
    real_close_all = FleetApp._close_all

    async def closing(self: FleetApp) -> None:
        await real_close_all(self)  # the screens are pruned: what a tick due now meets
        for name, callback in callbacks:
            try:
                callback()
            except Exception as exc:
                raised.append(f"{name}: {type(exc).__name__}: {exc}")

    monkeypatch.setattr(FleetApp, "_close_all", closing)

    async def run() -> None:
        app = FleetApp(
            refresh_seconds=3600,
            doctor=lambda: [],
            accounts=lambda: _overview(_status(1, "me@example.com")),
            headroom=lambda accounts: GOOD,
            headroom_seconds=3600,
        )
        async with app.run_test(size=(140, 40)):
            await settle_page(app)
            await app.on_project_selected(ProjectSelected("prj_a"))  # its Board, ticking
            await settle_page(app)
            await app.on_accounts_selected(AccountsSelected())  # the Accounts page, ticking
            await settle_page(app)
            accounts = app.query_one(AccountsView)
            welcome = app.query_one(WelcomeView)
            board = app.query_one(BoardPanel)
            app.action_remote_panel()  # the Remote panel, its one-second repaint
            await settle_page(app)
            remote = app.screen
            assert isinstance(remote, RemotePanel)
            callbacks.extend(
                [
                    ("FleetApp.refresh_data", app.refresh_data),
                    ("FleetApp.refresh_headroom", app.refresh_headroom),
                    ("AccountsView.refresh_readings", accounts.refresh_readings),
                    ("AccountsView._poll_login", accounts._poll_login),
                    ("WelcomeView._tick", welcome._tick),
                    ("BoardPanel._tick", board._tick),
                    ("RemotePanel.repaint", remote.repaint),
                ]
            )

    asyncio.run(run())
    assert len(callbacks) == 7, "the premise: every ticking callback was reached"
    assert raised == [], "\n".join(raised)
