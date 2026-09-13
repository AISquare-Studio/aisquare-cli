"""Project groups, pinning and manual order (#140): the store rows and the one arranger.

Every claim has its negative: a group shares nothing (no project row changes
but its layout fields); deleting a group ungroups and deletes no project; pins
outrank groups; positions stay dense; undo puts back exactly what a move touched.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import paths
from aisquare.core.store import ContextStore, store_session
from aisquare.models import ProjectInfo
from aisquare.services import project_groups as groups


def _project(store: ContextStore, name: str) -> ProjectInfo:
    return store.onboard_project(ProjectInfo(id=f"prj_{name}", root=Path("/w") / name))


@pytest.fixture
def store(isolated_home: Path) -> Iterator[ContextStore]:
    paths.ensure_home()
    with store_session() as opened:
        for name in ("api", "cli", "docs", "web"):
            _project(opened, name)
        yield opened


def _order(store: ContextStore) -> list[str]:
    return [p.id for p in groups.load_arrangement(store).ordered_projects()]


def _shape(store: ContextStore) -> dict[str, Any]:
    arrangement = groups.load_arrangement(store)
    return {
        "pinned": [
            e.group.name if isinstance(e, groups.GroupEntry) else e.id for e in arrangement.pinned
        ],
        "groups": {e.group.name: [m.id for m in e.members] for e in arrangement.groups},
        "loose": [p.id for p in arrangement.loose],
    }


def test_the_default_arrangement_is_the_old_one_by_name(store: ContextStore) -> None:
    assert _shape(store) == {
        "pinned": [],
        "groups": {},
        "loose": ["prj_api", "prj_cli", "prj_docs", "prj_web"],
    }


def test_groups_hold_members_in_manual_order_and_share_nothing(store: ContextStore) -> None:
    group, created = groups.create_group(store, "frontend", ["prj_web", "prj_docs"])
    assert group.name == "frontend" and created.groups == {group.id: None}
    assert _shape(store) == {
        "pinned": [],
        "groups": {"frontend": ["prj_web", "prj_docs"]},  # the order they were added, not by name
        "loose": ["prj_api", "prj_cli"],
    }
    web = store.get_project("prj_web")
    assert web is not None and web.group_id == group.id and web.position == 0
    # A second group lands after the first; a duplicate name is refused.
    groups.create_group(store, "backend", ["prj_api"])
    assert [g.name for g in store.project_groups()] == ["frontend", "backend"]
    with pytest.raises(ValueError, match="already exists"):
        groups.create_group(store, "Frontend".lower())
    # Nothing but layout fields moved: the project rows are otherwise the same.
    api = store.get_project("prj_api")
    assert api is not None and api.root == Path("/w/api") and api.onboarded_at is not None
    # Deleting a group ungroups (to the end of the top level) and deletes no project.
    entry = groups.delete_group(store, group.id)
    assert store.get_project_group(group.id) is None
    assert _shape(store)["loose"] == ["prj_cli", "prj_web", "prj_docs"]
    assert {p.id for p in store.list_projects()} == {"prj_api", "prj_cli", "prj_docs", "prj_web"}
    # …and undo brings the group back with its members, under its old id.
    assert groups.undo(store, entry) == "delete group frontend"
    assert _shape(store)["groups"] == {"backend": ["prj_api"], "frontend": ["prj_web", "prj_docs"]}
    assert store.get_project_group(group.id) is not None


def test_moves_are_positional_dense_and_undoable(store: ContextStore) -> None:
    groups.move_project(store, "prj_web", position=0)
    assert _order(store) == ["prj_web", "prj_api", "prj_cli", "prj_docs"]
    positions = [store.update_project_layout(pid).position for pid in _order(store)]
    assert positions == [0, 1, 2, 3], "dense after the first arrangement"
    groups.move_project(store, "prj_api", after="prj_docs")
    assert _order(store) == ["prj_web", "prj_cli", "prj_docs", "prj_api"]
    entry = groups.move_project(store, "prj_docs", before="prj_web")
    assert _order(store) == ["prj_docs", "prj_web", "prj_cli", "prj_api"]
    assert groups.undo(store, entry) == "move docs"
    assert _order(store) == ["prj_web", "prj_cli", "prj_docs", "prj_api"]
    # Steps: one place per press, clamped at the ends.
    groups.step(store, "prj_web", +1)
    assert _order(store) == ["prj_cli", "prj_web", "prj_docs", "prj_api"]
    groups.step(store, "prj_cli", -1)
    assert _order(store) == ["prj_cli", "prj_web", "prj_docs", "prj_api"]
    # Into a group at the end, then out again to the top level's end.
    group, _ = groups.create_group(store, "g")
    groups.move_project(store, "prj_web", to="g")
    assert _shape(store)["groups"] == {"g": ["prj_web"]}
    assert _shape(store)["loose"] == ["prj_cli", "prj_docs", "prj_api"]
    groups.move_project(store, "prj_cli", to=group.id, position=0)
    assert _shape(store)["groups"] == {"g": ["prj_cli", "prj_web"]}
    back = groups.remove_from_group(store, ["prj_cli"])
    assert _shape(store)["loose"] == ["prj_docs", "prj_api", "prj_cli"]
    groups.undo(store, back)
    assert _shape(store)["groups"] == {"g": ["prj_cli", "prj_web"]}
    with pytest.raises(KeyError):
        groups.move_project(store, "prj_web", to="no-such-group")
    with pytest.raises(KeyError):
        groups.move_project(store, "prj_nobody")


def test_pins_outrank_groups_and_keep_their_own_order(store: ContextStore) -> None:
    group, _ = groups.create_group(store, "g", ["prj_web", "prj_cli"])
    first = groups.pin(store, "prj_docs")
    groups.pin(store, "prj_web")  # pinned AND grouped: shown under Pinned, counted by its group
    groups.pin_group(store, group.id)
    shape = _shape(store)
    assert shape["pinned"] == ["prj_docs", "prj_web", "g"]  # pin order, not name order
    assert shape["groups"] == {} and shape["loose"] == ["prj_api"]
    entries = groups.load_arrangement(store).pinned
    pinned_group = next(e for e in entries if isinstance(e, groups.GroupEntry))
    assert [m.id for m in pinned_group.members] == ["prj_cli"], "a pinned member is listed once"
    assert pinned_group.group.collapsed is False, "a pinned group opens"
    groups.undo(store, first)
    assert _shape(store)["pinned"] == ["prj_web", "g"] and "prj_docs" in _shape(store)["loose"]
    groups.pin(store, "prj_web", pinned=False)
    assert _shape(store)["pinned"] == ["g"]
    groups.pin_group(store, group.id, pinned=False)
    assert _shape(store)["groups"] == {"g": ["prj_web", "prj_cli"]}


def test_unpinning_returns_a_project_to_a_dense_place(store: ContextStore) -> None:
    """Measured live before the fix: api, pinned while a group deletion appended web to
    the top level, came back at position 0 next to web's 0 — two rows, one slot."""
    group, _ = groups.create_group(store, "ui", ["prj_web"])
    groups.pin(store, "prj_api")
    groups.delete_group(store, group.id)  # web to the end of the top level, numbered without api
    unpin = groups.pin(store, "prj_api", pinned=False)
    loose = groups.load_arrangement(store).loose
    assert [p.id for p in loose] == ["prj_api", "prj_cli", "prj_docs", "prj_web"]
    assert [p.position for p in loose] == [0, 1, 2, 3], "dense: no shared slot, nothing by name"
    assert groups.undo(store, unpin) == "unpin api"
    assert _shape(store)["pinned"] == ["prj_api"], "undo re-pins it; the scope's numbers return"
    assert [p.position for p in groups.load_arrangement(store).loose] == [0, 1, 2]


def test_groups_reorder_rename_and_collapse(store: ContextStore) -> None:
    a, _ = groups.create_group(store, "a")
    b, _ = groups.create_group(store, "b")
    c, _ = groups.create_group(store, "c")
    assert [g.name for g in store.project_groups()] == ["a", "b", "c"]
    entry = groups.move_group(store, c.id, before="a")
    assert [g.name for g in store.project_groups()] == ["c", "a", "b"]
    groups.step_group(store, c.id, +1)
    assert [g.name for g in store.project_groups()] == ["a", "c", "b"]
    groups.undo(store, entry)
    assert [g.name for g in store.project_groups()] == ["a", "b", "c"]
    renamed = groups.rename_group(store, b.id, "  build ")
    assert store.get_project_group(b.id).name == "build"  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="already exists"):
        groups.rename_group(store, a.id, "build")
    with pytest.raises(ValueError, match="needs a name"):
        groups.rename_group(store, a.id, "   ")
    groups.undo(store, renamed)
    assert store.get_project_group(b.id).name == "b"  # type: ignore[union-attr]
    folded = groups.set_collapsed(store, a.id, True)
    assert store.get_project_group(a.id).collapsed is True  # type: ignore[union-attr]
    groups.undo(store, folded)
    assert store.get_project_group(a.id).collapsed is False  # type: ignore[union-attr]
    assert groups.resolve_group(store, "A").id == a.id  # case-insensitive by name
    with pytest.raises(KeyError):
        groups.resolve_group(store, "nope")


def test_a_project_arranged_by_nobody_lands_last(store: ContextStore) -> None:
    groups.move_project(store, "prj_web", position=0)
    with store_session() as other:
        _project(other, "aaa-new")  # onboarded later, alphabetically first
    assert _order(store)[0] == "prj_web" and _order(store)[-1] == "prj_aaa-new"
    stamp = datetime.now(tz=UTC) - timedelta(days=1)
    store.update_project_layout("prj_aaa-new", pinned_at=stamp)
    assert _order(store)[0] == "prj_aaa-new", "a pin is the one thing that beats manual order"
