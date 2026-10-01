"""With no hub exported, a project page's Explainability tab still speaks for the PAGE.

Review of #240, finding 12. A fleet window carries its fleet's own root as its
hub and names its row (#230), so the seats a page spawns join the page's project
and trace with the page's key, hub or no hub. #235 made the tab follow the page
under a hub and left the no-hub branch on ``team_project(page.root)``, which asks
git: for a page rooted at a sub-directory of a repository that is the ENCLOSING
repository's project. So the tab attached a key typed with *this project only* to
the repository, registered the roster under the repository's key, and said the
page's own key "is not used — … remove it", while the page's seats traced with
exactly that key.

The page here is ``repo/sub``, a registered project inside a real git repository
whose root ``repo`` is another registered project, and no hub is exported.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from textual.pilot import Pilot

from aisquare.cli.ui.views import explainability as explainability_view
from aisquare.cli.ui.views.explainability import ExplainabilityView
from aisquare.cli.ui.views.project import ProjectView
from aisquare.core import orchestrator
from aisquare.core.config import ExplainabilityTarget, load_config, save_config
from aisquare.core.store import store_session
from aisquare.core.workspace import project_id_for
from aisquare.models import FleetAgent, ProjectInfo
from aisquare.services import explainability as explainability_service
from aisquare.services import explainability_ops as ops
from tests import test_ui_project as project_suite
from tests.test_ui_project import Host, _attach_in_setup, drive, settle

# The project page suite's fixtures, bound here so pytest finds them for this module's
# tests: no manager and no tmux behind the page, no probe dialled.
no_fleet = project_suite.no_fleet
no_real_tmux = project_suite.no_real_tmux
quiet_explainability = project_suite.quiet_explainability

PAGE_KEY = "pk-page-0123456789"


def _registered(root: Path) -> ProjectInfo:
    info = ProjectInfo(id=project_id_for(root.resolve()), root=root.resolve(), linked_repos=[])
    with store_session() as store:
        return store.onboard_project(info)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjectInfo:
    """A real git repository, registered: the project git names for every directory in it."""
    root = tmp_path / "repo"
    (root / "sub").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    monkeypatch.chdir(root)
    return _registered(root)


@pytest.fixture
def page(repo: ProjectInfo) -> ProjectInfo:
    """The page: ``repo/sub``, a project of its own inside the repository."""
    return _registered(repo.root / "sub")


def _joined_by_a_seat_of(page: ProjectInfo, monkeypatch: pytest.MonkeyPatch) -> ProjectInfo:
    """The project a seat spawned for ``page`` joins, resolved inside its window's environment.

    What ``fleet spawn`` puts on the window (``services.fleet.spawn``): the fleet's own
    root as the hub, and the row's id.
    """
    with store_session() as store:
        seat = store.upsert_fleet_agent(
            FleetAgent(
                id="agt_seatforthepage",
                project_id=page.id,
                label="coder-1",
                role="coder",
                pane_id="%1",
                cwd=page.root,
                created_at=datetime.now(tz=UTC),
            )
        )
    with monkeypatch.context() as window:
        window.setenv("AISQUARE_TEAM_HUB", str(page.root))
        window.setenv("AISQUARE_FLEET_AGENT", seat.id)
        return orchestrator.team_project()


def test_with_no_hub_the_tabs_key_project_is_the_page_as_its_seats_join_it(
    repo: ProjectInfo, page: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pin. git names the enclosing repository for the page's root, which is what the
    tab answered; a seat of the page joins the page."""
    assert orchestrator.team_hub() is None, "no hub is exported"
    assert orchestrator.team_project(page.root).id == repo.id != page.id, "what git answers"

    tab = explainability_view.key_project(page)

    assert tab is not None and tab.id == page.id
    assert _joined_by_a_seat_of(page, monkeypatch).id == tab.id


def test_with_no_hub_a_key_saved_from_the_tab_attaches_to_the_page(
    repo: ProjectInfo, page: ProjectInfo
) -> None:
    """*This project only* is the page: the key went to the repository around it, which
    no seat of the page reads."""

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[list[tuple[str, str]], str]:
        host.query_one(ProjectView).active = "tab-explainability"
        await settle(pilot)
        _attach_in_setup(host, PAGE_KEY)
        await settle(pilot)
        return list(host.notices), host.query_one(ExplainabilityView).status_text

    notices, status = drive(page, scenario)

    [attached] = [m for m, _ in notices if m.startswith("✓ key attached to")]
    assert attached.startswith("✓ key attached to sub for target stg"), attached
    with store_session() as store:
        assert store.project_explainability(page.id) is not None
        assert store.project_explainability(repo.id) is None, "never the repository's"
    assert explainability_service.project_key_path(page.id).read_text(encoding="utf-8") == PAGE_KEY
    assert "sub: its own key for target stg (in use)" in status, status


def test_with_no_hub_the_pages_own_key_reads_as_in_use_and_is_never_called_unused(
    repo: ProjectInfo, page: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row said "sub: its own key for target stg is not used — its launches join repo;
    remove it with: …" about the key the page's seats authenticate with."""
    ops.attach_project_key(page, PAGE_KEY, target="stg")

    row = dict(explainability_view.status_report(page).rows)["project"]

    assert row == "sub: its own key for target stg (in use)", row
    assert "is not used" not in row and "remove it" not in row and "repo" not in row
    # And it IS the key a seat of the page resolves: the row is true of them.
    seat_project = _joined_by_a_seat_of(page, monkeypatch)
    resolved = ops.resolve_target(load_config().explainability, None, project_id=seat_project.id)
    assert (resolved.api_key, resolved.key_source) == (PAGE_KEY, "project")


def test_with_no_hub_register_roster_declares_under_the_pages_key(
    repo: ProjectInfo, page: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """*Register roster* resolved the repository's key (none here, so it refused "not
    set") while the page's seats send spans under the page's: 409 on every one."""
    config = load_config()
    config.explainability.targets = {"stg": ExplainabilityTarget(gateway_url="https://stg.example")}
    save_config(config)
    ops.attach_project_key(page, PAGE_KEY, target="stg")
    keys: list[str | None] = []

    def register_roster(target: ops.ResolvedTarget, names: tuple[str, ...]) -> ops.HttpVerdict:
        keys.append(target.api_key)
        return ops.HttpVerdict(ok=True, status=200, detail="HTTP 200", payload={"agents": []})

    monkeypatch.setattr(ops, "register_roster", register_roster)

    notice = explainability_view.register_roster(page)

    assert keys == [PAGE_KEY], notice.message
    assert notice.message.startswith("✓ registered"), notice.message
