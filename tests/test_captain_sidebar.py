"""The captain's row in the fleet UI: under a home-level heading, never inside a project (T2).

Acceptance (contract 13121, item 6): the sidebar shows a "Captain" section above
the projects with the captain's agent row; the home never gets a project card;
selecting the row opens the same agent view any agent opens. Driven with the
UI suite's harness: the real store in the isolated home, ``list_agents``
scripted, and every tmux call held to a private socket.
"""

from __future__ import annotations

from pathlib import Path

from textual.pilot import Pilot

from aisquare.cli.ui.sidebar import AgentRow
from aisquare.cli.ui.views.agent import AgentView
from aisquare.services.captain import state as captain_state
from tests import test_ui_shell as ui_suite
from tests.test_ui_shell import Script, drive, fleet_app, row_for, seed, shown, status

# The UI suite's fixtures, bound here so pytest finds them for this module's tests
# (``no_real_tmux`` is autouse there: every tmux call held to a private socket).
no_real_tmux = ui_suite.no_real_tmux
script = ui_suite.script


def test_the_captain_row_sits_under_a_home_heading_not_inside_a_project(
    tmp_path: Path,
    script: Script,
) -> None:
    seed(tmp_path, ("prj_aaa", "alpha", "amber-otter"))
    home = captain_state.home_project()
    captain = status(home.id, "captain", "captain", "waiting")
    script[home.id] = [captain]
    script["prj_aaa"] = [status("prj_aaa", "coder-1", "coder", "working")]

    async def body(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        app.refresh_data()
        await pilot.pause()
        section = app.query_one("#captain-section")
        row = row_for(app, captain.agent.id)
        assert section in row.ancestors, "the captain's row is in the home-level section"
        assert "captain" in shown(row)
        assert not app.query(f"#card-{home.id}"), "the home is never a project card"
        assert app.query("#card-prj_aaa"), "the projects are still listed"
        row.activate()
        await pilot.pause()
        view = app.query_one(AgentView)
        assert view.status.agent.id == captain.agent.id, "selecting it opens its agent view"

    drive(body)


def test_with_no_captain_the_section_says_how_to_start_one(tmp_path: Path, script: Script) -> None:
    seed(tmp_path, ("prj_aaa", "alpha", "amber-otter"))

    async def body(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        app.refresh_data()
        await pilot.pause()
        section = app.query_one("#captain-section")
        assert not section.query(AgentRow)
        assert "aisquare captain" in shown(app.query_one("#captain-empty"))  # type: ignore[arg-type]

    drive(body)
