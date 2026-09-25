"""Project groups, pinning and manual order (#140): the store rows and the one arranger.

Every claim has its negative: a group shares nothing (no project row changes
but its layout fields); deleting a group ungroups and deletes no project; pins
outrank groups; positions stay dense; undo puts back exactly what a move touched.
"""

from __future__ import annotations

import sqlite3
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


def test_a_step_and_a_position_count_the_rows_the_list_shows(store: ContextStore) -> None:
    """A captured directory is hidden from the sidebar and ``project list`` (#139), so it is
    not a place: counted, a shift+↓ swapped api with it — nothing moved on screen, and the
    gesture still took an undo — and ``--position 1`` landed behind it (review of #171)."""
    store.ensure_project(ProjectInfo(id="prj_bench", root=Path("/w/bench")))  # captured only
    shown = [p.id for p in store.list_projects()]
    assert "prj_bench" not in shown and store.get_project("prj_bench") is not None

    def listed() -> list[str]:
        return [pid for pid in _order(store) if pid in shown]

    groups.step(store, "prj_api", +1)
    assert listed() == ["prj_cli", "prj_api", "prj_docs", "prj_web"], "one place on screen"
    groups.step(store, "prj_api", -1)
    assert listed() == ["prj_api", "prj_cli", "prj_docs", "prj_web"]
    groups.move_project(store, "prj_web", position=1)
    assert listed() == ["prj_api", "prj_web", "prj_cli", "prj_docs"], "index 1 of the list"
    # Every row of the scope is still numbered, the hidden one included: dense, no ties.
    everything = groups.load_arrangement(store, all=True).loose
    assert [p.position for p in everything] == list(range(len(everything)))
    # With the captured rows on screen (the sidebar's `a`), they are places again.
    first = everything[0].id
    groups.step(store, first, +1, all=True)
    assert groups.load_arrangement(store, all=True).loose[1].id == first
    # At either end there is nowhere to go: nothing moves and nothing is remembered.
    top = groups.step(store, listed()[0], -1)
    assert top.description == "nothing to move" and not top.projects


def test_undo_puts_a_project_at_the_top_level_when_its_group_was_deleted_since(
    store: ContextStore,
) -> None:
    """``u`` in the sidebar after the group the project came from was deleted from a shell:
    the old row names a group that is gone, and writing it back failed on the foreign key
    halfway through the restore (review of #171). It lands at the top level instead."""
    group, _ = groups.create_group(store, "tools", ["prj_cli", "prj_docs"])
    out = groups.move_project(store, "prj_cli", to=groups.TOP, position=0)
    groups.delete_group(store, group.id)  # another surface, between the gesture and the undo
    assert groups.undo(store, out) == "move cli"
    cli = store.get_project("prj_cli")
    assert cli is not None and cli.group_id is None
    assert set(_order(store)) == {"prj_api", "prj_cli", "prj_docs", "prj_web"}


def test_undo_brings_back_only_a_group_its_own_change_deleted(store: ContextStore) -> None:
    """A group the entry only remembered — its place among the groups, its fold — that was
    deleted from a shell before the `u` stays deleted: re-created, it came back empty. The
    entry of the deletion is the one that brings it back, members and all."""
    tools, _ = groups.create_group(store, "tools", ["prj_cli"])
    site, _ = groups.create_group(store, "site", ["prj_web"])
    moved = groups.move_group(store, site.id, position=0)  # remembers tools's place too
    folded = groups.set_collapsed(store, tools.id, True)
    gone = groups.delete_group(store, tools.id)  # another surface, before the undo
    assert groups.undo(store, folded) == "collapse tools"
    assert groups.undo(store, moved) == "move group site"
    assert [g.name for g in store.project_groups()] == ["site"], "tools stays deleted"
    assert groups.undo(store, gone) == "delete group tools"
    assert _shape(store)["groups"] == {"tools": ["prj_cli"], "site": ["prj_web"]}


def test_a_forgotten_project_leaves_the_arrangement_and_comes_back_like_a_new_one(
    store: ContextStore,
) -> None:
    """A forget kept the row's group, place and pin, and the next prompt in the directory
    revives the row (#139): added again, it came back pinned and grouped, at a number its
    scope had given away while it was forgotten — two rows at 0, ordered by name (review of
    #171, round 1). The forget takes it out of the arrangement, and an undo of a gesture
    made before the forget does not write its old place back onto the tombstone."""
    groups.create_group(store, "tools", ["prj_api", "prj_cli", "prj_docs"])
    pinned = groups.pin(store, "prj_api")
    store.forget_project("prj_api")
    groups.move_project(store, "prj_docs", position=0)  # tools renumbered without api
    assert groups.undo(store, pinned) == "pin api"
    api = ProjectInfo(id="prj_api", root=Path("/w/api"))
    store.ensure_project(api)  # a prompt there: back, captured
    store.onboard_project(api)  # added again on purpose
    revived = store.get_project("prj_api")
    assert revived is not None
    assert (revived.group_id, revived.position, revived.pinned_at) == (None, None, None)
    assert _shape(store) == {
        "pinned": [],
        "groups": {"tools": ["prj_docs", "prj_cli"]},
        "loose": ["prj_web", "prj_api"],
    }, "loose, and last, where a project arranged by nobody lands"
    tools = groups.load_arrangement(store).groups[0].members
    assert [p.position for p in tools] == [0, 1], "no shared slot"


def test_an_undo_after_a_change_from_elsewhere_puts_the_row_back_without_a_tie(
    store: ContextStore,
) -> None:
    """An undo writes back the numbers its rows had. A move from a shell between the
    gesture and its `u` renumbered the scope, and the restored row landed on a number
    another row held by then: two rows at 0, ordered by name — web behind docs (review of
    #171, round 2). It gets its place back, as an unpin does, and the scope stays dense.
    A row forgotten and added again in between is put back the same way."""
    groups.create_group(store, "tools", ["prj_web", "prj_cli", "prj_docs"])
    pinned = groups.pin(store, "prj_web")
    groups.move_project(store, "prj_docs", position=0)  # tools renumbered without web
    assert groups.undo(store, pinned) == "pin web"
    tools = groups.load_arrangement(store).groups[0].members
    assert [(p.id, p.position) for p in tools] == [("prj_web", 0), ("prj_docs", 1), ("prj_cli", 2)]

    again = groups.pin(store, "prj_web")
    store.forget_project("prj_web")
    groups.move_project(store, "prj_cli", position=0)
    web = ProjectInfo(id="prj_web", root=Path("/w/web"))
    store.ensure_project(web)
    store.onboard_project(web)  # back, loose, before the `u`
    assert groups.undo(store, again) == "pin web"
    tools = groups.load_arrangement(store).groups[0].members
    assert [(p.id, p.position) for p in tools] == [("prj_web", 0), ("prj_cli", 1), ("prj_docs", 2)]


def test_a_forgotten_project_cannot_be_arranged_and_an_old_tombstone_comes_back_loose(
    store: ContextStore,
) -> None:
    """A gesture from a stale frame — the sidebar between two refreshes, the group picker
    left open — while a shell ran ``project forget`` was written onto the tombstone, and the
    next prompt there revived the project pinned and grouped. It is refused as a project
    that is gone. A tombstone an older forget left arranged comes back loose and unpinned,
    by a capture or by an onboard; a live row keeps its place (review of #171, round 2)."""
    groups.create_group(store, "tools", ["prj_api", "prj_cli"])
    store.forget_project("prj_api")
    with pytest.raises(KeyError):
        groups.pin(store, "prj_api")
    with pytest.raises(KeyError):
        groups.move_project(store, "prj_api", to="tools", position=0)
    with pytest.raises(KeyError):
        store.update_project_layout("prj_api", pinned_at=datetime.now(tz=UTC))

    groups.pin(store, "prj_cli")
    groups.create_group(store, "site", ["prj_web", "prj_docs"])
    raw = sqlite3.connect(str(paths.db_path()))
    try:  # the forget as it was before round 1, the tombstone keeping its place, before v23
        raw.execute(
            "UPDATE project SET forgotten_at = ?, onboarded_at = NULL "
            "WHERE id IN ('prj_cli', 'prj_web')",
            (datetime.now(tz=UTC).isoformat(),),
        )
        raw.execute("PRAGMA user_version = 22")
        raw.commit()
    finally:
        raw.close()
    with store_session():
        pass  # the next open repairs such a tombstone, once (v23)
    store.ensure_project(ProjectInfo(id="prj_cli", root=Path("/w/cli")))  # a prompt there
    store.onboard_project(ProjectInfo(id="prj_web", root=Path("/w/web")))  # added on purpose
    for project_id in ("prj_cli", "prj_web"):
        revived = store.get_project(project_id)
        assert revived is not None
        assert (revived.group_id, revived.position, revived.pinned_at) == (None, None, None)
    store.ensure_project(ProjectInfo(id="prj_docs", root=Path("/w/docs")))
    store.onboard_project(ProjectInfo(id="prj_docs", root=Path("/w/docs")))
    assert _shape(store)["groups"] == {"tools": [], "site": ["prj_docs"]}, "a live row stays"


def test_a_project_forgotten_mid_move_drops_out_and_the_move_completes(
    store: ContextStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forget from another process between a move's read of its scope and its writes: a
    layout write to the tombstone is refused now, and the refusal cut the move off
    halfway, one scope renumbered and the other not. The forgotten row drops out, and the
    rest is numbered."""
    groups.create_group(store, "tools", ["prj_cli", "prj_docs"])
    read = groups._scope_members
    reads: list[str | None] = []

    def then_forgotten(opened: ContextStore, group_id: str | None) -> list[ProjectInfo]:
        members = read(opened, group_id)
        reads.append(group_id)
        if len(reads) == 2:  # both scopes read, nothing written yet
            opened.forget_project("prj_api")
        return members

    monkeypatch.setattr(groups, "_scope_members", then_forgotten)
    groups.move_project(store, "prj_cli", to=groups.TOP, position=0)
    monkeypatch.undo()
    assert _shape(store)["groups"] == {"tools": ["prj_docs"]}
    assert _shape(store)["loose"] == ["prj_cli", "prj_web"]
    assert [p.position for p in groups.load_arrangement(store).loose] == [0, 2]


def _layout(opened: ContextStore) -> dict[str, Any]:
    """Every project's and group's place: group, position, pin; and each group's position."""
    return {
        "projects": {
            p.id: (p.group_id, p.position, p.pinned_at) for p in opened.list_projects(all=True)
        },
        "groups": {g.id: (g.name, g.position) for g in opened.project_groups()},
    }


@pytest.mark.parametrize("change", ["move", "group move", "several moves"])
def test_a_change_the_store_refuses_part_way_leaves_the_layout_as_it_was(
    store: ContextStore, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """A move is several writes, and each was committed on its own. When the store
    refused the second one (a FOREIGN KEY failure on a group deleted from a shell since
    the move read it), the first stayed committed: a half-applied layout, and the TUI
    recorded no undo entry for it because the move raised (review of #203). Each change
    is one transaction now, a gesture of several moves included, so a refusal leaves
    the layout exactly as it was, in this connection and on disk."""
    tools, _ = groups.create_group(store, "tools", ["prj_web"])
    groups.create_group(store, "ops")
    groups.create_group(store, "misc")
    before = _layout(store)
    method = "update_project_group" if change == "group move" else "update_project_layout"
    real = getattr(store, method)
    writes: list[str] = []

    def refused(ident: str, **fields: Any) -> Any:
        if fields:
            writes.append(ident)
            second = change != "several moves" and len(writes) == 2
            # The second project's move, as it writes that project into the group.
            into_group = ident == "prj_cli" and fields.get("group_id") == tools.id
            if second or (change == "several moves" and into_group):
                raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")
        return real(ident, **fields)

    misc = _group_id(store, "misc")
    # Scoped, never `monkeypatch.undo()`: that undoes the isolated home as well, and the
    # fresh session below would then open the real ~/.aisquare store.
    with monkeypatch.context() as patched, pytest.raises(sqlite3.IntegrityError):
        patched.setattr(store, method, refused)
        if change == "move":
            groups.move_project(store, "prj_api", to="tools")
        elif change == "group move":
            groups.move_group(store, misc, position=0)
        else:
            groups.add_to_group(store, "tools", ["prj_api", "prj_cli"])
    assert len(writes) >= 2, "the refusal came after a write had been made"
    assert _layout(store) == before
    with store_session() as fresh:
        assert _layout(fresh) == before, "a part of the change was committed"


def _group_id(store: ContextStore, name: str) -> str:
    return groups.resolve_group(store, name).id
