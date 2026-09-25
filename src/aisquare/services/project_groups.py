"""Project groups, pinning and manual order — a management layer over projects (#140).

Browsers solved this for tabs: a named, collapsible container that is purely for
listing and managing, a way to keep a few things at the top, and the freedom to
arrange the rest by hand. Nothing here changes what is INSIDE a project: context
entries, prompt history, snapshots, boards and explainability settings stay per
project, and no other table carries a group id. Order, pins and collapse state
live in the store (schema v20), not in ``state.json``.

One place computes the order every surface shows (:func:`arrange`), one place
applies each change (:func:`move_project`, :func:`move_group`, :func:`pin`,
:func:`create_group`, …) as one transaction, and every change returns an :class:`UndoEntry` —
the rows' layout as it was — so the sidebar's ``u`` and a CLI mistake have the
same way back (:func:`undo`). Positions are integers per scope (the top level,
or one group), renumbered densely after every move, so two projects never tie.
A pin or a forget can leave a gap, which orders the same: every placement is by
index, never by number.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Concatenate, ParamSpec, TypeVar

from aisquare.core.store import ContextStore
from aisquare.models import ProjectGroup, ProjectInfo

TOP = "top"
"""The scope name for "no group" in ``move_project(to=…)``."""

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _one_transaction(
    change: Callable[Concatenate[ContextStore, _P], _R],
) -> Callable[Concatenate[ContextStore, _P], _R]:
    """Apply ``change`` as one transaction (``ContextStore.layout_change``): all of it, or none.

    A change here is several writes, and each committed on its own: a move that
    the store refused part-way (a FOREIGN KEY failure on a group deleted from a
    shell since the move read it) left the scope it renumbered first committed
    and the rest not, with no undo entry for the part that landed, because the
    change raised (review of #203). Changes that call each other (a group made
    with its members, a step) nest into the outermost one.
    """

    @functools.wraps(change)
    def applied(store: ContextStore, /, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        with store.layout_change():
            return change(store, *args, **kwargs)

    return applied


@dataclass(frozen=True)
class GroupEntry:
    """A group with its (unpinned) members in display order.

    ``pinned_members`` are the members listed under Pinned instead: not shown
    under the header, still the group's, so its roll-up counts them. Left out
    of the entry, a header said nothing of an agent asking for the user in a
    pinned member (review of #171, round 1).
    """

    group: ProjectGroup
    members: list[ProjectInfo]
    pinned_members: list[ProjectInfo] = field(default_factory=list)


@dataclass(frozen=True)
class Arrangement:
    """What the sidebar and ``project list`` show, in order.

    ``pinned`` holds projects and groups by pin order; ``groups`` the unpinned
    groups by position; ``loose`` the unpinned, ungrouped projects by position.
    A pinned project is shown in the Pinned section even if it also belongs to a
    group — the pin is the stronger statement — and its group counts it in the
    roll-up but not in the list.
    """

    pinned: list[ProjectInfo | GroupEntry]
    groups: list[GroupEntry]
    loose: list[ProjectInfo]

    def ordered_projects(self) -> list[ProjectInfo]:
        """Every project once, in the order the sidebar walks them."""
        out: list[ProjectInfo] = []
        seen: set[str] = set()

        def take(project: ProjectInfo) -> None:
            if project.id not in seen:
                seen.add(project.id)
                out.append(project)

        for entry in self.pinned:
            if isinstance(entry, GroupEntry):
                for member in entry.members:
                    take(member)
            else:
                take(entry)
        for entry in self.groups:
            for member in entry.members:
                take(member)
        for project in self.loose:
            take(project)
        return out


def _by_position(projects: list[ProjectInfo]) -> list[ProjectInfo]:
    return sorted(
        projects,
        key=lambda p: (p.position is None, p.position or 0, (p.root.name or p.id).lower()),
    )


def arrange(projects: list[ProjectInfo], groups: list[ProjectGroup]) -> Arrangement:
    """The display order: pinned first, then groups by position, then the loose projects."""
    pinned_projects = [p for p in projects if p.pinned_at is not None]
    pinned_groups = [g for g in groups if g.pinned_at is not None]
    members: dict[str, list[ProjectInfo]] = {g.id: [] for g in groups}
    pinned_members: dict[str, list[ProjectInfo]] = {g.id: [] for g in groups}
    loose: list[ProjectInfo] = []
    for project in projects:
        if project.pinned_at is not None:
            if project.group_id is not None and project.group_id in pinned_members:
                pinned_members[project.group_id].append(project)
            continue
        if project.group_id is not None and project.group_id in members:
            members[project.group_id].append(project)
        else:
            loose.append(project)
    entries = {
        g.id: GroupEntry(g, _by_position(members[g.id]), pinned_members[g.id]) for g in groups
    }
    pinned: list[tuple[datetime, ProjectInfo | GroupEntry]] = [
        (p.pinned_at, p) for p in pinned_projects if p.pinned_at is not None
    ] + [(g.pinned_at, entries[g.id]) for g in pinned_groups if g.pinned_at is not None]
    pinned.sort(key=lambda item: item[0])
    return Arrangement(
        pinned=[item for _, item in pinned],
        groups=[
            entries[g.id]
            for g in sorted(groups, key=lambda g: (g.position, g.name))
            if g.pinned_at is None
        ],
        loose=_by_position(loose),
    )


def load_arrangement(store: ContextStore, *, all: bool = False) -> Arrangement:
    return arrange(store.list_projects(all=all), store.project_groups())


# --- undo -----------------------------------------------------------------------------------


@dataclass
class UndoEntry:
    """A change's way back: the layout of every row it touched, as it was.

    ``groups`` maps a group id to its row before the change, or ``None`` when
    the group did not exist yet (undo deletes it). A group the change deleted
    (``deleted_groups``) is re-created under its old id, so its members' rows
    can point at it again; one it only remembered — its place, its fold — and
    that is gone by the undo was deleted since, and stays deleted.
    """

    description: str
    projects: dict[str, tuple[str | None, int | None, datetime | None]] = field(
        default_factory=dict
    )
    groups: dict[str, ProjectGroup | None] = field(default_factory=dict)
    deleted_groups: set[str] = field(default_factory=set)


def _remember(store: ContextStore, entry: UndoEntry, project_ids: list[str]) -> None:
    known = {p.id: p for p in store.list_projects(all=True)}
    for project_id in project_ids:
        if project_id in entry.projects:
            continue
        project = known.get(project_id)
        if project is not None:
            entry.projects[project_id] = (project.group_id, project.position, project.pinned_at)


def _remember_group(store: ContextStore, entry: UndoEntry, group_id: str) -> None:
    if group_id not in entry.groups:
        entry.groups[group_id] = store.get_project_group(group_id)


@_one_transaction
def undo(store: ContextStore, entry: UndoEntry) -> str:
    """Put every row the entry names back; returns what was undone."""
    for group_id, before in entry.groups.items():
        current = store.get_project_group(group_id)
        if before is None:
            if current is not None:
                store.delete_project_group(group_id)
            continue
        if current is None:
            if group_id not in entry.deleted_groups:
                # Deleted from a shell between the gesture and its `u`, by nothing
                # this entry did: re-created, it came back empty (review of #171).
                continue
            store.create_project_group(before.name, group_id=group_id)
        store.update_project_group(
            group_id,
            name=before.name,
            position=before.position,
            pinned_at=before.pinned_at,
            collapsed=before.collapsed,
        )
    restored: set[str] = set()
    scopes: set[str | None] = set()
    for project_id, (scope, position, pinned_at) in entry.projects.items():
        if scope is not None and store.get_project_group(scope) is None:
            # Its group was deleted since — from a shell, between the sidebar's
            # gesture and its `u` — and is not one this entry re-creates. Written
            # back as it was, the row failed the foreign key halfway through the
            # restore (review of #171, round 1): it goes to the top level, last,
            # where a deleted group's members go.
            scope, position = None, None
        try:
            store.update_project_layout(
                project_id, group_id=scope, position=position, pinned_at=pinned_at
            )
        except KeyError:
            # Forgotten or purged since the gesture. A forget takes the row out of
            # the arrangement; written back onto the tombstone, its old group,
            # number and pin came back with it when a prompt revived the row
            # (review of #171, round 1).
            continue
        restored.add(project_id)
        scopes.add(scope)
    for scope in scopes:
        _untie(store, scope, first=restored)
    return entry.description


def _untie(store: ContextStore, group_id: str | None, *, first: set[str]) -> None:
    """Renumber a scope an undo wrote into, when two of its rows now share a number.

    An undo writes back the numbers its rows had. A change from elsewhere
    between a gesture and its `u` — a move from a shell, a forget and a
    revival — renumbers the scope meanwhile, and a restored row's old number can
    be another row's by then: two rows on one slot, ordered by name (review of
    #171, round 2). The rows ``first`` names — the restored ones — win the tie,
    and the scope is numbered densely around them: back at the place they had,
    as an unpin puts a project back at its own.
    """
    members = _scope_members(store, group_id)
    numbers = [p.position for p in members if p.position is not None]
    if len(numbers) == len(set(numbers)):
        return
    ordered = sorted(
        members, key=lambda p: (p.position is None, p.position or 0, p.id not in first)
    )
    _renumber(store, ordered, group_id)


# --- renumbering ------------------------------------------------------------------------------


def _scope_members(store: ContextStore, group_id: str | None) -> list[ProjectInfo]:
    """The unpinned projects of a scope, in display order."""
    arrangement = load_arrangement(store, all=True)
    if group_id is None:
        return list(arrangement.loose)
    for entry in [
        *arrangement.groups,
        *(e for e in arrangement.pinned if isinstance(e, GroupEntry)),
    ]:
        if entry.group.id == group_id:
            return list(entry.members)
    return []


def _shown(projects: list[ProjectInfo], *, all: bool) -> list[ProjectInfo]:
    """The rows a list shows: captured directories are hidden unless ``all`` (#139)."""
    return list(projects) if all else [p for p in projects if p.onboarded_at is not None]


def _renumber(store: ContextStore, ordered: list[ProjectInfo], group_id: str | None) -> None:
    """Write the scope's order back: dense positions, every row in the scope's group.

    Every row is written, not only the ones that look changed: the project
    being moved arrives as a copy already carrying its new group, so comparing
    against it would skip the one write that matters (measured: a project that
    "joined" a group in memory and stayed loose in the store).
    """
    for index, project in enumerate(ordered):
        try:
            store.update_project_layout(project.id, group_id=group_id, position=index)
        except KeyError:
            continue  # forgotten since the scope was read: it has left the arrangement


def _insert_at(
    ordered: list[ProjectInfo],
    project: ProjectInfo,
    *,
    before: str | None,
    after: str | None,
    position: int | None,
) -> list[ProjectInfo]:
    rest = [p for p in ordered if p.id != project.id]
    if before is not None:
        index = next((i for i, p in enumerate(rest) if p.id == before), len(rest))
    elif after is not None:
        index = next((i + 1 for i, p in enumerate(rest) if p.id == after), len(rest))
    elif position is not None:
        index = max(0, min(int(position), len(rest)))
    else:
        index = len(rest)
    return [*rest[:index], project, *rest[index:]]


# --- the moves ------------------------------------------------------------------------------


def resolve_group(store: ContextStore, ref: str) -> ProjectGroup:
    """A group by name or id; ``KeyError`` when none."""
    found = store.get_project_group(ref) or store.find_project_group(ref)
    if found is None:
        raise KeyError(ref)
    return found


@_one_transaction
def move_project(
    store: ContextStore,
    project_id: str,
    *,
    to: str | None = None,
    before: str | None = None,
    after: str | None = None,
    position: int | None = None,
    all: bool = False,
) -> UndoEntry:
    """Put a project into a scope (``to``: a group, ``"top"``, or ``None`` = stay) at a place.

    ``before`` / ``after`` name a sibling in the target scope; ``position`` is
    an index; none of them means the END of the scope — where a newly grouped
    project lands, like a new tab. A pinned project keeps its pin: the pin is
    the stronger statement, and moving it changes where it goes back to when
    unpinned.

    ``position`` counts the rows the list SHOWS: a captured directory it hides
    (#139) is not a place, unless ``all`` — the sidebar's ``a`` — shows it.
    Counted, ``--position 1`` over a hidden row put the project behind it,
    which on screen was no move at all (review of #171, round 1). Every row of
    the scope is still renumbered, hidden ones included, so none ties.
    """
    current = store.update_project_layout(project_id)  # a read, and a KeyError when unknown
    if to is None:
        target_group: str | None = current.group_id
    elif to == TOP:
        target_group = None
    else:
        target_group = resolve_group(store, to).id
    if before is not None:
        before = store.update_project_layout(before).id
    if after is not None:
        after = store.update_project_layout(after).id
    entry = UndoEntry(f"move {current.root.name or current.id}")
    old_scope = _scope_members(store, current.group_id)
    new_scope = _scope_members(store, target_group)
    if position is not None and before is None and after is None:
        places = [p for p in _shown(new_scope, all=all) if p.id != project_id]
        index = max(0, int(position))
        before, position = (places[index].id if index < len(places) else None), None
    _remember(store, entry, [p.id for p in old_scope] + [p.id for p in new_scope] + [project_id])
    if target_group != current.group_id:
        _renumber(store, [p for p in old_scope if p.id != project_id], current.group_id)
    moved = current.model_copy(update={"group_id": target_group})
    ordered = _insert_at(new_scope, moved, before=before, after=after, position=position)
    _renumber(store, ordered, target_group)
    return entry


@_one_transaction
def move_group(
    store: ContextStore,
    group_id: str,
    *,
    before: str | None = None,
    after: str | None = None,
    position: int | None = None,
) -> UndoEntry:
    """Reorder a group among the top-level groups (pinned groups keep their pin order)."""
    group = store.get_project_group(group_id)
    if group is None:
        raise KeyError(group_id)
    groups = [g for g in store.project_groups() if g.pinned_at is None]
    entry = UndoEntry(f"move group {group.name}")
    for g in groups:
        _remember_group(store, entry, g.id)
    _remember_group(store, entry, group_id)
    rest = [g for g in groups if g.id != group_id]
    if before is not None:
        target = resolve_group(store, before).id
        index = next((i for i, g in enumerate(rest) if g.id == target), len(rest))
    elif after is not None:
        target = resolve_group(store, after).id
        index = next((i + 1 for i, g in enumerate(rest) if g.id == target), len(rest))
    elif position is not None:
        index = max(0, min(int(position), len(rest)))
    else:
        index = len(rest)
    ordered = [*rest[:index], group, *rest[index:]]
    for slot, g in enumerate(ordered):
        if g.position != slot:
            store.update_project_group(g.id, position=slot)
    return entry


@_one_transaction
def pin(store: ContextStore, project_id: str, pinned: bool = True) -> UndoEntry:
    """Pin a project into the Pinned section, or return it to its place.

    Pinning only stamps ``pinned_at``: the row keeps its group and position, so
    the roll-up still counts it and an unpin knows where it came from.

    Unpinning re-inserts the project at the position it left (clamped to the
    scope as it is now) and renumbers the scope. While it was pinned the scope
    was renumbered WITHOUT it — a group deletion, a move — so its old number can
    be taken by now. Measured live before this branch did so: ``api`` pinned,
    a group deleted (its member appended to the top level at 0), ``api``
    unpinned: two rows at position 0, their order decided by name instead of
    by anyone.
    """
    project = store.update_project_layout(project_id)
    entry = UndoEntry(f"{'pin' if pinned else 'unpin'} {project.root.name or project.id}")
    if pinned:
        _remember(store, entry, [project_id])
        store.update_project_layout(project_id, pinned_at=datetime.now(tz=UTC))
        return entry
    scope = _scope_members(store, project.group_id)
    _remember(store, entry, [project_id, *(member.id for member in scope)])
    store.update_project_layout(project_id, pinned_at=None)
    ordered = _insert_at(scope, project, before=None, after=None, position=project.position)
    _renumber(store, ordered, project.group_id)
    return entry


def pin_group(store: ContextStore, group_id: str, pinned: bool = True) -> UndoEntry:
    group = store.get_project_group(group_id)
    if group is None:
        raise KeyError(group_id)
    entry = UndoEntry(f"{'pin' if pinned else 'unpin'} group {group.name}")
    _remember_group(store, entry, group_id)
    store.update_project_group(
        group_id,
        pinned_at=datetime.now(tz=UTC) if pinned else None,
        collapsed=False if pinned else None,
    )
    return entry


def set_collapsed(store: ContextStore, group_id: str, collapsed: bool) -> UndoEntry:
    group = store.get_project_group(group_id)
    if group is None:
        raise KeyError(group_id)
    entry = UndoEntry(f"{'collapse' if collapsed else 'expand'} {group.name}")
    _remember_group(store, entry, group_id)
    store.update_project_group(group_id, collapsed=collapsed)
    return entry


@_one_transaction
def create_group(
    store: ContextStore, name: str, project_ids: Sequence[str] = ()
) -> tuple[ProjectGroup, UndoEntry]:
    """A new group at the end of the top level, optionally with its first members."""
    clean = name.strip()
    if not clean:
        raise ValueError("a group needs a name")
    group = store.create_project_group(clean)
    entry = UndoEntry(f"create group {clean}")
    entry.groups[group.id] = None
    for project_id in project_ids:
        member = move_project(store, project_id, to=group.id)
        for pid, layout in member.projects.items():
            entry.projects.setdefault(pid, layout)
    return group, entry


def rename_group(store: ContextStore, group_id: str, name: str) -> UndoEntry:
    clean = name.strip()
    if not clean:
        raise ValueError("a group needs a name")
    group = store.get_project_group(group_id)
    if group is None:
        raise KeyError(group_id)
    entry = UndoEntry(f"rename group {group.name}")
    _remember_group(store, entry, group_id)
    store.update_project_group(group_id, name=clean)
    return entry


@_one_transaction
def delete_group(store: ContextStore, group_id: str) -> UndoEntry:
    """Delete a group; its members go back to the top level, at the end, in their order."""
    group = store.get_project_group(group_id)
    if group is None:
        raise KeyError(group_id)
    entry = UndoEntry(f"delete group {group.name}")
    _remember_group(store, entry, group_id)
    entry.deleted_groups.add(group_id)
    members = _scope_members(store, group_id)
    pinned_members = [
        p
        for p in store.list_projects(all=True)
        if p.group_id == group_id and p.pinned_at is not None
    ]
    loose = _scope_members(store, None)
    _remember(
        store,
        entry,
        [p.id for p in members] + [p.id for p in pinned_members] + [p.id for p in loose],
    )
    store.delete_project_group(group_id)
    _renumber(store, [*loose, *members], None)
    return entry


@_one_transaction
def add_to_group(store: ContextStore, group_ref: str, project_ids: Sequence[str]) -> UndoEntry:
    group = resolve_group(store, group_ref)
    entry = UndoEntry(f"group {len(project_ids)} project(s) into {group.name}")
    for project_id in project_ids:
        part = move_project(store, project_id, to=group.id)
        for pid, layout in part.projects.items():
            entry.projects.setdefault(pid, layout)
    return entry


@_one_transaction
def remove_from_group(store: ContextStore, project_ids: Sequence[str]) -> UndoEntry:
    entry = UndoEntry(f"ungroup {len(project_ids)} project(s)")
    for project_id in project_ids:
        part = move_project(store, project_id, to=TOP)
        for pid, layout in part.projects.items():
            entry.projects.setdefault(pid, layout)
    return entry


def step(store: ContextStore, project_id: str, delta: int, *, all: bool = False) -> UndoEntry:
    """Move a project one place up (-1) or down (+1) inside its scope — the keyboard's move.

    One place is one place ON SCREEN: the project goes past the next row the
    list shows, stepping over the captured directories it hides (unless
    ``all``). Swapped with a hidden row, a shift+↓ changed nothing anyone could
    see and still took an undo (review of #171, round 1). With nowhere to go —
    an end of the scope, or a pinned project, which has no place in one — the
    entry is "nothing to move" and remembers no row.
    """
    project = store.update_project_layout(project_id)
    ids = [p.id for p in _shown(_scope_members(store, project.group_id), all=all)]
    if project_id not in ids:
        return UndoEntry("nothing to move")
    index = ids.index(project_id)
    target = max(0, min(len(ids) - 1, index + delta))
    if target == index:
        return UndoEntry("nothing to move")
    if target < index:
        return move_project(store, project_id, before=ids[target])
    return move_project(store, project_id, after=ids[target])


def step_group(store: ContextStore, group_id: str, delta: int) -> UndoEntry:
    groups = [g for g in store.project_groups() if g.pinned_at is None]
    ids = [g.id for g in groups]
    if group_id not in ids:
        return UndoEntry("nothing to move")
    index = ids.index(group_id)
    target = max(0, min(len(ids) - 1, index + delta))
    if target == index:
        return UndoEntry("nothing to move")
    return move_group(store, group_id, position=target)
