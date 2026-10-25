"""A parent manager over a project group (P2-W6, #199 tier 2).

A manager could not hear past its own board: a note on a member's board reached the manager
coordinating several projects only when it next read that board, because every nudge stays
on the board it was written on. A group (#140) can now name a **parent**, an ordinary project
(a coordination directory, not a member) whose manager is the group's, and route chosen
events of its members up to it:

- **Route up** (:func:`route_up`), inside the member's own board write: ``routed`` on the
  parent's board, addressed to its manager (so P1 WS-2's ``wake_class`` wakes it), one line
  and a pointer, ``routed from <codename>#<seq> (<kind> by <who>): <first line, 120 chars> —
  aisquare board --project <codename>``; then the parent manager's nudge. What routes is
  the group's rules (``project group route``): kinds (by default :data:`DEFAULT_KINDS`:
  questions to the manager or the parent, ``task_blocked``, ``result``, and every status
  change of a task delegated from the parent), a regex on a note's text, and anything
  ``--to parent``. A storm guard: one routed event per child per :data:`ROUTE_WINDOW`, the
  rest folded into a count the next one carries; the nudge keeps its own 5 s debounce.
- **Linked tasks**: a task added on a member with ``--origin <parent task>`` records it
  (``team_task.origin_task_id``); its status changes route up, and once every task delegated
  from an origin is done, ``linked_all_done`` lands on the origin's board. The parent closes
  the origin itself.
- **Subtree pause** (:func:`pause_group`): the parent and every member paused at once; an
  agent-issued spawn (``--as``) on a paused board is refused (``services.fleet.spawn``).
- **Cap**: ``project_group.max_agents``, the live agents across the members and the parent,
  checked with W1a's admission.
- **Briefing**: a parent's manager is told its group in at most :data:`BRIEFING_MAX` chars.

A group without a parent behaves byte for byte as before. Boards stay per project.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from aisquare.core.store import ContextStore, GroupSettings
from aisquare.models import ProjectGroup, ProjectInfo, TeamEvent

DEFAULT_KINDS: tuple[str, ...] = ("question", "task_blocked", "result", "linked")
"""What routes up by default. ``question`` is a question to the manager, to the parent, or
to nobody in particular; ``linked`` is every status change of a task delegated from the
parent."""
ROUTE_KINDS = frozenset({*DEFAULT_KINDS, "task_done", "task_review", "task_reopened", "note"})
"""What ``project group route --kinds`` takes."""
ROUTE_WINDOW = timedelta(seconds=60)
"""One routed event per child per window; the rest are folded into a count."""
ROUTED_TEXT = 120
BRIEFING_MAX = 600
PARENT = "parent"
"""The addressee ``note --to parent`` names."""
_NOTE_KINDS = frozenset({"note", "decision", "question", "result"})
_TASK_KINDS = frozenset(
    {"task_review", "task_done", "task_blocked", "task_reopened", "task_dropped"}
)
_URGENT = frozenset({"question", "task_blocked"})
"""What the storm guard never folds: what waits on a decision (with anything ``--to parent``)."""
_NEVER = frozenset({"routed", "linked_all_done"})
"""What never routes again: a routed line on a parent that is itself a member stays put."""


def _now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(frozen=True)
class Parent:
    """A member's group, its settings and its parent project."""

    group: ProjectGroup
    settings: GroupSettings
    project: ProjectInfo


def parent_of(store: ContextStore, project_id: str) -> Parent | None:
    """The parent over ``project_id``'s group, or ``None``: no group, no parent, or the
    project is the parent itself."""
    project = store.get_project(project_id, include_forgotten=True)
    if project is None or project.group_id is None:
        return None
    settings = store.group_settings(project.group_id)
    parent_id = settings.parent_project_id
    if parent_id is None or parent_id == project_id:
        return None
    group = store.get_project_group(project.group_id)
    parent = store.get_project(parent_id)
    if group is None or parent is None:
        return None
    return Parent(group, settings, parent)


def groups_parented_by(store: ContextStore, project_id: str) -> list[ProjectGroup]:
    """The groups whose parent is ``project_id``."""
    return [
        group
        for group in store.project_groups()
        if store.group_settings(group.id).parent_project_id == project_id
    ]


def _rules(settings: GroupSettings) -> tuple[frozenset[str], re.Pattern[str] | None, bool]:
    route = settings.route or {}
    kinds = route.get("kinds")
    chosen = (
        frozenset(str(k) for k in kinds) if isinstance(kinds, list) else frozenset(DEFAULT_KINDS)
    )
    pattern = route.get("pattern")
    try:
        compiled = re.compile(str(pattern)) if pattern else None
    except re.error:
        compiled = None
    return chosen, compiled, bool(route.get("off"))


def routes(store: ContextStore, event: TeamEvent, parent: Parent) -> bool:
    """Whether the group's rules send ``event`` up (see the module)."""
    if event.kind in _NEVER:
        return False
    kinds, pattern, off = _rules(parent.settings)
    if off:
        return False
    to = (event.to_role or "").strip().lower()
    if to == PARENT:
        return True
    if event.kind == "question" and "question" in kinds and to in ("", "manager", PARENT):
        return True
    if event.kind in kinds and event.kind != "question":
        return True
    if (
        "linked" in kinds
        and event.kind in _TASK_KINDS
        and event.task_id is not None
        and event.task_id in store.task_origins(event.project_id)
    ):
        return True
    return bool(pattern and event.kind in _NOTE_KINDS and pattern.search(event.text))


def _who(store: ContextStore, event: TeamEvent) -> str:
    if event.session_id is None:
        return "cli"
    agent = store.fleet_agent_for_session(event.session_id)
    if agent is not None:
        return agent.label
    session = store.get_session(event.session_id)
    return (session.label or session.role) if session is not None else "?"


def routed_text(store: ContextStore, event: TeamEvent, member: ProjectInfo, folded: int) -> str:
    """One line and a pointer: what a parent's manager reads of a member's event."""
    name = member.codename or member.id
    first = event.text.strip().splitlines()[0] if event.text.strip() else ""
    if len(first) > ROUTED_TEXT:
        first = first[: ROUTED_TEXT - 1].rstrip() + "…"
    more = f", +{folded} folded" if folded else ""
    return (
        f"routed from {name}#{event.seq} ({event.kind} by {_who(store, event)}{more}): {first}"
        f" — aisquare board --project {name}"
    )


def _guard_key(project_id: str) -> str:
    return f"route:{project_id}"


def route_up(store: ContextStore, event: TeamEvent) -> list[str]:
    """Inside the member's own board write: append ``routed`` on the parent's board when the
    rules say so (or fold it into the count while the storm guard holds), and
    ``linked_all_done`` on an origin's board when its last delegated task closed. Returns the
    projects whose managers to nudge once the write is committed."""
    from aisquare.services import team as team_service  # lazy: team imports this module

    wake: list[str] = []
    parent = parent_of(store, event.project_id)
    if parent is not None and routes(store, event, parent):
        member = store.get_project(event.project_id, include_forgotten=True)
        key = _guard_key(event.project_id)
        raw = store.get_meta(key)
        try:
            kept = json.loads(raw) if raw else {}
        except ValueError:
            kept = {}
        now = _now()
        last = kept.get("at")
        folded = int(kept.get("folded") or 0)
        # What asks for a decision is never folded: a question, a block, anything sent to
        # the parent by name. The cycle escalates there now (review of #381, 2).
        urgent = event.kind in _URGENT or (event.to_role or "").strip().lower() == PARENT
        if not urgent and last and now - datetime.fromisoformat(str(last)) < ROUTE_WINDOW:
            store.set_meta(key, json.dumps({"at": last, "folded": folded + 1}))
        elif member is not None:
            team_service._emit(
                store,
                parent.project.id,
                "routed",
                routed_text(store, event, member, folded),
                to_role="manager",
                meta={"from": event.project_id, "seq": event.seq, "kind": event.kind},
            )
            store.set_meta(key, json.dumps({"at": now.isoformat(), "folded": 0}))
            wake.append(parent.project.id)
    if event.kind in ("task_done", "task_dropped") and event.task_id is not None:
        origin = store.task_origins(event.project_id).get(event.task_id)
        if origin is not None and _all_done(store, origin[0]):
            origin_task = store.get_task(origin[0])
            if origin_task is not None and store.get_meta(f"linked_all_done:{origin[0]}") is None:
                number = f"#{origin_task.number}" if origin_task.number else origin_task.id
                linked = store.linked_tasks(origin[0])
                dropped = sum(1 for task in linked if task.status == "dropped")
                closed = (
                    f"are closed ({len(linked) - dropped} done, {dropped} dropped)"
                    if dropped
                    else "are done"
                )
                team_service._emit(
                    store,
                    origin[1],
                    "linked_all_done",
                    f"all {len(linked)} tasks delegated from {number} {closed}: "
                    f"close {number} when it is ({origin_task.title})",
                    task_id=origin[0],
                    to_role="manager",
                )
                store.set_meta(f"linked_all_done:{origin[0]}", _now().isoformat())
                wake.append(origin[1])
    return wake


def _all_done(store: ContextStore, origin_task_id: str) -> bool:
    """Every task delegated from the origin is closed and at least one is done: a dropped
    child does not hold the origin open (review of #381)."""
    linked = store.linked_tasks(origin_task_id)
    closed = {"done", "dropped"}
    return any(t.status == "done" for t in linked) and all(t.status in closed for t in linked)


# --- the parent's briefing --------------------------------------------------------------


def briefing(store: ContextStore, project_id: str) -> list[str]:
    """A parent manager's block (at most :data:`BRIEFING_MAX` chars): its groups' members,
    their managers, and the delegation verbs. Nothing for a project that parents no group."""
    groups = groups_parented_by(store, project_id)
    if not groups:
        return []
    lines = ["You are the parent manager of:"]
    for group in groups:
        members = [
            p
            for p in store.list_projects(all=True)
            if p.group_id == group.id and p.id != project_id
        ]
        named = []
        for member in members:
            manager = store.fleet_agent_by_label(member.id, "manager", live_only=True)
            named.append(f"{member.codename or member.id}{' (manager up)' if manager else ''}")
        lines.append(f"  {group.name}: {', '.join(named) or 'no members yet'}")
    lines += [
        'Delegate with `aisquare task add "…" --project <member> --origin <task>`; members\'',
        "questions, blocks, results and linked tasks reach you as `routed` lines.",
    ]
    text = "\n".join(lines)
    if len(text) > BRIEFING_MAX:
        text = text[: BRIEFING_MAX - 1] + "…"
    return text.splitlines()


# --- the group verbs ----------------------------------------------------------------------


def validate_kinds(kinds: Sequence[str]) -> list[str]:
    bad = sorted(set(kinds) - ROUTE_KINDS)
    if bad:
        raise ValueError(
            f"unknown kind(s) {', '.join(bad)}: one of {', '.join(sorted(ROUTE_KINDS))}"
        )
    return list(dict.fromkeys(kinds))


def route_settings(
    current: dict[str, Any] | None,
    *,
    kinds: Sequence[str] | None,
    pattern: str | None,
    off: bool,
) -> dict[str, Any]:
    """The rules ``project group route`` writes: ``current`` with what was given changed."""
    route = dict(current or {})
    if kinds is not None:
        route["kinds"] = validate_kinds(kinds)
    if pattern is not None:
        if pattern:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"--pattern is not a regular expression: {exc}") from exc
            route["pattern"] = pattern
        else:
            route.pop("pattern", None)
    route["off"] = off
    return route


# --- subtree pause and the cap ----------------------------------------------------------


def group_projects(store: ContextStore, group_id: str) -> list[ProjectInfo]:
    """A group's members and its parent: what ``fleet pause --group`` pauses."""
    members = [p for p in store.list_projects(all=True) if p.group_id == group_id]
    parent_id = store.group_settings(group_id).parent_project_id
    parent = store.get_project(parent_id) if parent_id else None
    return [*members, *([parent] if parent is not None and parent not in members else [])]


def _covering(store: ContextStore, project_id: str) -> list[tuple[str, GroupSettings]]:
    """The groups whose cap counts ``project_id``: its own group, and every group it parents."""
    project = store.get_project(project_id, include_forgotten=True)
    found: dict[str, GroupSettings] = {}
    if project is not None and project.group_id is not None:
        found[project.group_id] = store.group_settings(project.group_id)
    for group in groups_parented_by(store, project_id):
        found[group.id] = store.group_settings(group.id)
    return list(found.items())


def _live_in(store: ContextStore, group_id: str) -> list[Any]:
    ids = {p.id for p in group_projects(store, group_id)}
    return sorted(
        (a for a in store.live_fleet_agents() if a.project_id in ids and a.role != "captain"),
        key=lambda agent: (agent.created_at, agent.id),
    )


def cap_refusal(store: ContextStore, project_id: str) -> str | None:
    """Why one more agent in ``project_id`` would pass a group's cap
    (``project_group.max_agents``), or ``None``."""
    for group_id, settings in _covering(store, project_id):
        if settings.max_agents <= 0:
            continue
        live = _live_in(store, group_id)
        if len(live) >= settings.max_agents:
            group = store.get_project_group(group_id)
            name = group.name if group is not None else group_id
            return (
                f"group {name} already runs {len(live)} agents (its cap is "
                f"{settings.max_agents}) — stop one, or `aisquare project group cap {name} <n>`"
            )
    return None


def over_cap(store: ContextStore, agent: Any) -> str | None:
    """The group cap ``agent``'s just-written row is past, settled the way the project cap
    is (``fleet._verify_cap``): the live rows in ``(created_at, id)`` order, so N parallel
    spawns at a group's last seat settle to exactly the cap. ``None`` when it is within."""
    for group_id, settings in _covering(store, agent.project_id):
        if settings.max_agents <= 0:
            continue
        live = _live_in(store, group_id)
        if len(live) > settings.max_agents and any(
            a.id == agent.id for a in live[settings.max_agents :]
        ):
            group = store.get_project_group(group_id)
            return (
                f"that would be {len(live)} agents in group "
                f"{group.name if group is not None else group_id} (its cap is "
                f"{settings.max_agents}) — another spawn took the last seat"
            )
    return None


# --- linked tasks on a board ------------------------------------------------------------


def link_marks(store: ContextStore, project_id: str) -> dict[str, str]:
    """Task id → the mark its board line ends with (P2-W6): ``← <origin board>#<n>`` for a
    task delegated from another board, ``→ <k> linked, <d> done`` for a task others were
    delegated from. Empty for a board with neither (two indexed reads)."""
    marks: dict[str, str] = {}
    for task_id, (origin_id, origin_project) in store.task_origins(project_id).items():
        origin = store.get_task(origin_id)
        board = store.get_project(origin_project, include_forgotten=True)
        name = (board.codename or board.id) if board is not None else origin_project
        number = f"#{origin.number}" if origin is not None and origin.number else origin_id
        marks[task_id] = f"← {name}{number}"
    for origin_id, (total, done) in store.linked_counts(project_id).items():
        marks[origin_id] = f"→ {total} linked, {done} done"
    return marks
