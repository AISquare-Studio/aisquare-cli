"""The aisquare home, added as a project, is never packed into a snapshot.

The owner's decision, 2026-10-23, following D5: once the home can be a project (``init``,
``project onboard`` or asq's ``+`` there), a Repomix pack of it would copy aisquare's
settings, the fleet database and every Claude account's login data into a file the
session-start hook points every agent at. So ``init``, ``project onboard`` and any later
``--refresh`` never pack it; a pack left from before is neither read nor served, and the
next onboard deletes it; agents there get no packed overview, as in a project without a
snapshot. An ordinary project still gets its snapshot.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aisquare.core import paths
from aisquare.core import snapshot as snapshot_core
from aisquare.core.workspace import project_id_for
from aisquare.models import ProjectInfo
from aisquare.services import diagnostics, hooks, lifecycle
from aisquare.services import project as project_service

PACK = '<files>\n<file path="secret.json">\n{"token": "sk-..."}\n</file>\n</files>\n'
POINTER = "packed snapshot"


@pytest.fixture(autouse=True)
def packer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A packer that works, so only the home rule can stop a pack."""
    monkeypatch.setattr(
        snapshot_core, "_run_repomix", lambda _root, *, compress, ignore=(): (PACK, "Total: 9")
    )
    monkeypatch.setattr(snapshot_core, "_total_tokens", lambda _text, _out: 100)


@pytest.fixture
def home() -> ProjectInfo:
    root = paths.aisquare_home().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return ProjectInfo(id=project_id_for(root), root=root)


@pytest.fixture
def ordinary(tmp_path: Path) -> ProjectInfo:
    root = tmp_path / "work"
    root.mkdir()
    return ProjectInfo(id=project_id_for(root.resolve()), root=root.resolve())


def test_an_ordinary_project_is_packed_and_its_agents_are_pointed_at_it(
    ordinary: ProjectInfo,
) -> None:
    report = project_service.onboard(ordinary.root, refresh=False)

    assert report.snapshot is not None and report.snapshot.status == "ready"
    assert POINTER in hooks._directive(ordinary.id, has_prompts=False)


@pytest.mark.parametrize("refresh", [False, True], ids=["onboard", "refresh"])
def test_onboarding_the_home_packs_nothing_and_points_no_agent_at_a_pack(
    home: ProjectInfo, refresh: bool
) -> None:
    report = project_service.onboard(home.root, refresh=refresh)

    assert report.snapshot is None and report.snapshot_note == snapshot_core.HOME_SKIPPED
    assert not snapshot_core.snapshot_dir(home.id).exists()
    assert POINTER not in hooks._directive(home.id, has_prompts=False)


def test_init_in_the_home_packs_nothing(home: ProjectInfo) -> None:
    report = lifecycle.initialize(
        home.root,
        api_key=None,
        local=True,
        agents=[],
        onboard=True,
        reinit=False,
        assume_yes=True,
        explainability=False,
    )
    assert not snapshot_core.snapshot_dir(home.id).exists()
    assert any(snapshot_core.HOME_SKIPPED in note for note in report.notes), report.notes


def test_a_pack_of_the_home_from_before_is_never_served_and_the_next_onboard_deletes_it(
    home: ProjectInfo,
) -> None:
    snapshot_core.generate(home.id, home.root)  # what an earlier version could leave
    assert snapshot_core.meta_path(home.id).exists()

    assert snapshot_core.load(home.id) is None and not snapshot_core.exists(home.id)
    assert POINTER not in hooks._directive(home.id, has_prompts=False)

    project_service.onboard(home.root, refresh=False)
    assert not snapshot_core.snapshot_dir(home.id).exists()


def test_doctor_reads_the_homes_missing_snapshot_as_ok(home: ProjectInfo) -> None:
    project_service.onboard(home.root, refresh=False)

    check = diagnostics._check_snapshot(home.root)

    assert check.status == "ok", check
    assert check.detail == snapshot_core.HOME_SKIPPED
