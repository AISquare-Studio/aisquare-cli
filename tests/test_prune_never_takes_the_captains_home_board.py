"""``project prune --captured-only`` never takes the captain's home board, and doctor never
counts it among the captured directories.

Review of #240, "also confirmed" (Low): ``project prune --captured-only --purge`` can purge
the home board. The aisquare home's row is the captain's home board
(``services.captain.state.home_project``): captured on purpose, so that it never joins the
owner's projects, and never onboarded. To ``--captured-only`` that is a scratch directory a
session merely ran in, so once the captain sat idle past ``--older-than`` the prune dropped
it, and ``--purge`` deleted the captain's board and its audit; doctor's captured row counted
it and advised that prune. Reached with the captain off too, on any machine where it ever ran.
"""

from __future__ import annotations

from pathlib import Path

from aisquare.core.store import store_session
from aisquare.models import ProjectInfo
from aisquare.services import diagnostics
from aisquare.services import project as project_service
from aisquare.services.captain import state as captain_state


def test_a_captured_only_prune_keeps_the_home_board_and_takes_a_scratch_directory(
    tmp_path: Path,
) -> None:
    home = captain_state.home_project()  # the captain's board, captured as its first use does
    with store_session() as store:
        store.ensure_project(ProjectInfo(id="prj_scratch", root=tmp_path / "scratch"))

    plan = project_service.prune_candidates(missing=False, worktrees=False, captured_older_than=0)

    dropped = [candidate.project.id for candidate in plan]
    assert "prj_scratch" in dropped, "the control: a scratch directory is still pruned"
    assert home.id not in dropped


def test_doctor_counts_no_home_board_among_the_captured_directories(tmp_path: Path) -> None:
    captain_state.home_project()
    with store_session() as store:
        store.ensure_project(ProjectInfo(id="prj_scratch", root=tmp_path / "scratch"))
        captured = [project.id for project in store.captured_projects()]

    [check] = diagnostics._check_captured_projects()

    assert captured == ["prj_scratch"]
    assert " ".join(check.detail.split()).startswith("1 captured directory"), check.detail
