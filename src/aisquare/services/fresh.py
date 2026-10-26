"""Start fresh (``project fresh``): clear an onboarded project's board and fleet, and start it
a blank manager, without onboarding it again.

Two steps, so a surface can ask before it acts: :func:`plan` reads what would go and refuses
what must not be done, changing nothing; :func:`start_fresh` asks the same again and does
it. ``project fresh`` and the UI's Start fresh… call these two.

What goes, scoped to the one project: its board (tasks, events, sessions, its signals and
the gone sessions' marks, ``store.reset_project``), its fleet (each live agent stopped as
``fleet shutdown`` stops it, each agent's worktree removed, then its rows), and with the
extras asked for, its context entries (``memory``), its prompt log and turn metrics
(``prompts``) and its local ``fleet/<codename>/…`` branches (``branches``). What stays: the
checkout and every other branch, the registration (name, nickname, group, pin, settings,
codename), the onboarding snapshot, the accounts, the captain, and every other project.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from aisquare.core import orchestrator
from aisquare.core.store import ContextStore, is_the_home, store_session
from aisquare.models import FreshCounts, FreshPlan, FreshReport, FreshUnpushed, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import project as project_service


class FreshRefused(Exception):
    """Start fresh will not run, and nothing was touched. ``code`` says why, for ``--json``:
    ``not_onboarded``, ``the_home``, ``inside_fleet_agent``, ``inside_fleet`` (asked from
    inside the fleet's tmux server, whose sessions the stop would kill), ``project_busy``
    (another project's fleet reads its board), ``fleet_error`` (the fleet or git could not
    be asked), ``unpushed_work`` (with the ``plan`` that found it)."""

    def __init__(self, message: str, *, code: str, plan: FreshPlan | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.plan = plan


class FreshIncomplete(Exception):
    """Start fresh stopped part-way, and nothing on the board was cleared: an agent or a
    tmux session stood after the stop, work nobody agreed to discard was found once it had
    run (with the ``plan`` that found it), or a later step failed. ``report`` says what was
    done before it."""

    def __init__(self, message: str, report: FreshReport, *, plan: FreshPlan | None = None) -> None:
        super().__init__(message)
        self.report = report
        self.plan = plan


Discard = Sequence[FreshUnpushed] | Literal["all"]
"""What :func:`start_fresh` may throw away of the work found nowhere else: the items someone
confirmed (``ConfirmDiscard``'s list, the CLI's at a terminal), each at the tip it was shown
at; ``"all"``, whatever the run finds (``project fresh --yes --discard-unpushed``, said
explicitly, off a terminal); or nothing."""


def plan(
    project: ProjectInfo, *, memory: bool = False, prompts: bool = False, branches: bool = False
) -> FreshPlan:
    """What :func:`start_fresh` would clear, stop and remove, read-only, and the work found
    only in the trees and branches it removes (:func:`fleet_service.unpushed_work`).

    Refused (:class:`FreshRefused`) as the run would be: a project not onboarded (or the
    aisquare home, the captain's board), a call from inside one of its own fleet agents,
    whose pane the stop would take down mid-reset, another project's fleet agents that read
    its board as a hub's, live or a restart away (``project forget``'s rule,
    :func:`project_service.board_readers`), and a fleet the stop could not take down from
    here (``fleet shutdown``'s own refusals: no usable tmux, a call from inside its server).

    The tmux sessions the stop kills are planned too (:func:`_sessions`), live rows or none:
    a session outlives its rows (a dead pane ``remain-on-exit`` keeps, a process a row
    recorded as died that still runs), and left standing it kept them beside the new
    manager."""
    with store_session() as store:
        project = _refuse(store, project)
        rows = store.fleet_agents(project.id)
        tables = store.reset_project(project.id, memory=memory, prompts=prompts, apply=False)
    live = sorted(
        (row for row in rows if row.ended_at is None),
        key=lambda row: row.label != fleet_service.MANAGER_LABEL,
    )
    try:
        sessions = _sessions(project, live=bool(live))
        trees = fleet_service.fleet_trees(project, rows)
        tips = fleet_service.fleet_branch_tips(project, rows) if branches else {}
        unpushed = fleet_service.unpushed_work(project, trees, tips)
    except fleet_service.FleetError as exc:
        raise FreshRefused(f"can't start fresh: {exc}", code="fleet_error") from exc
    return FreshPlan(
        project=project,
        memory=memory,
        prompts=prompts,
        branches=branches,
        counts=_counts(tables, worktrees=len(trees), branches=len(tips) if branches else None),
        live_agents=[row.label for row in live],
        sessions=sessions,
        worktrees=trees,
        fleet_branches=tips,
        unpushed=unpushed,
    )


def start_fresh(
    project: ProjectInfo,
    *,
    memory: bool = False,
    prompts: bool = False,
    branches: bool = False,
    discard: Discard = (),
    actor: str = "user",
    ack_wait: float = 0.0,
) -> FreshReport:
    """Clear ``project``'s board and fleet, and start it a fresh manager.

    Planned again first (:func:`plan`, whose refusals are this one's), and refused while any
    tree or branch it would remove holds work found nowhere else that ``discard`` does not
    cover (:func:`_uncovered`): a confirmed list covers each item at the tip it was shown
    at, and no uncommitted change it did not show. Then, in this order, so that a step that
    fails leaves what a run again can finish: the live agents stopped and the project's tmux
    sessions killed (``fleet shutdown``'s path, whenever either is there: claims released,
    ``agent_exited`` said); what the trees and branches hold asked again, now that nothing
    runs there, and refused as before; the agents' worktrees removed (``--force`` only for
    work it may discard), with ``branches`` the fleet branches deleted, the rows cleared in
    one transaction (``store.reset_project``), and a manager spawned as ``fleet spawn
    manager`` spawns one, a new session id and the only one on the board.

    :class:`FreshIncomplete`, with the report so far and nothing on the board cleared, when
    the stop left an agent or a session standing (no tree is removed then, nor a manager
    started beside the old session), when work nobody agreed to discard is found after the
    stop, or when a later step fails (a store that will not take the write lock). A
    manager the fleet refuses to start is said on the report (``manager_error``), the board
    clear all the same. ``actor`` is who asked, on each ``agent_exited``; ``ack_wait`` is
    the spawn's.
    """
    planned = plan(project, memory=memory, prompts=prompts, branches=branches)
    # Tips and all, before anything is stopped: an agent that committed while the dialog was
    # open is refused here with its fleet still running. Checked after the stop alone, the
    # likeliest case left the project stopped, its session killed and its claims released
    # (tester-fresh-ui at a14eafc1).
    if _uncovered(planned.unpushed, discard):
        raise FreshRefused(unpushed_refusal(planned), code="unpushed_work", plan=planned)
    project = planned.project
    report = FreshReport(project=project, planned=planned.counts)
    stopped = bool(planned.live_agents or planned.sessions)
    if stopped:
        _stop(project, report, actor=actor)
    try:
        # Inside the try: its read opens the store, and a store locked past its busy timeout
        # after the stop lost the report of what was stopped (review of #429, round 5).
        _confirm_down(project, report, planned.sessions, stopped=stopped)
        found, trees, tips = _held_now(project, report, branches=branches)
        if _uncovered(found, discard):
            later = planned.model_copy(
                update={"unpushed": found, "worktrees": trees, "fleet_branches": tips}
            )
            raise FreshIncomplete(found_after_stop(later), report, plan=later)
        report.worktrees_removed, kept = fleet_service.remove_fleet_trees(
            project, trees, force=bool(found)
        )
        report.kept += kept
        if branches:
            report.branches_deleted, kept = fleet_service.delete_fleet_branches(project, tips)
            report.kept += kept
        report.discarded = [
            item
            for item in found
            if item.path in report.worktrees_removed
            or (item.path is None and item.branch in report.branches_deleted)
        ]
        with store_session() as store:
            tables = store.reset_project(project.id, memory=memory, prompts=prompts, apply=True)
    except (FreshIncomplete, FreshRefused):
        raise
    except ValueError as exc:  # reset_project: an agent live again
        raise FreshIncomplete(
            f"{exc}, started since its fleet was stopped — the board was not cleared. Stop "
            f"it ({project_service.fleet_command('shutdown', project)}), then start fresh "
            "again",
            report,
        ) from exc
    except Exception as exc:  # a store that will not take the write lock, a git that hung
        raise FreshIncomplete(
            f"stopped part-way ({type(exc).__name__}: {exc}) — the board was not cleared; "
            "start fresh again",
            report,
        ) from exc
    report.cleared = _counts(
        tables,
        worktrees=len(report.worktrees_removed),
        branches=len(report.branches_deleted) if branches else None,
    )
    try:
        receipt = fleet_service.spawn(project, "manager", spawned_by=actor, ack_wait=ack_wait)
    except fleet_service.FleetError as exc:
        report.manager_error = str(exc)
    except Exception as exc:  # the board is clear: the report is owed whatever the spawn hit
        report.manager_error = f"{type(exc).__name__}: {exc}"
    else:
        report.manager = receipt.agent
    return report


def _stop(project: ProjectInfo, report: FreshReport, *, actor: str) -> None:
    """Stop the project's fleet as ``fleet shutdown`` does, onto ``report``, and raise
    :class:`FreshIncomplete` unless it is wholly down: a row left live, a session tmux would
    not kill, or a project the shutdown could not confirm down. Read on ``failed`` alone, a
    session tmux would not kill was passed over, and the trees went, the board was cleared
    and a manager started beside the session (review of #429, M3)."""
    name = project_service.display_name(project)
    try:
        down = fleet_service.shutdown(project, actor=actor)
    except fleet_service.FleetInterrupted as exc:
        report.stopped = _stopped(exc.report)
        report.sessions_killed = list(exc.report.sessions_killed)
        report.sessions_failed = list(exc.report.sessions_failed)
        raise FreshIncomplete(
            f"interrupted while stopping {name}'s fleet — the board was not cleared", report
        ) from None
    except fleet_service.FleetError as exc:
        raise FreshRefused(f"can't start fresh: {exc}", code="fleet_error") from exc
    report.stopped = _stopped(down)
    report.sessions_killed = list(down.sessions_killed)
    report.sessions_failed = list(down.sessions_failed)
    if not (down.failed or down.sessions_failed or project.id in down.incomplete_projects):
        return
    standing = [f"{row.agent.label} ({row.reason})" for row in down.failed]
    standing += [f"tmux session {session}" for session in down.sessions_failed]
    what = "; ".join(standing) or "the stop could not confirm it down"
    shutdown = project_service.fleet_command("shutdown", project)
    raise FreshIncomplete(
        f"{name}'s fleet is not down — {what}. Nothing was removed and the board was not "
        f"cleared: take it down with {shutdown} (whose --forget ends the rows of agents you "
        "know are not running), then start fresh again",
        report,
    )


def _confirm_down(
    project: ProjectInfo, report: FreshReport, planned: Sequence[str], *, stopped: bool
) -> None:
    """Go on only once tmux confirms the project has no session standing, asked strictly
    (``project_sessions``), after the stop and when none ran; then record as gone each
    session the plan listed that the stop did not kill (``sessions_gone``).

    Inferred from the stop's report ("neither killed nor failed" read as gone), a session
    the kill phase passed over, asked just in case and failing to answer, was said to have
    ended while it stood with its process; and a plan whose just-in-case ask failed listed
    none, no stop ran, and the reset went on over it (review of #429, round 4, N2). A session
    standing, or one tmux cannot say of, is :class:`FreshIncomplete` once the fleet was
    stopped, :class:`FreshRefused` when nothing was, with nothing removed either way."""
    standing = fleet_service.project_sessions(project)
    if standing == []:
        report.sessions_gone = [
            session for session in planned if session not in report.sessions_killed
        ]
        return
    if standing is None:
        what = (
            f"tmux could not say whether {fleet_service.session_name(project.codename or '')} "
            "stands"
        )
    else:
        what = f"tmux session {', '.join(standing)} still stands"
    shutdown = project_service.fleet_command("shutdown", project)
    said = (
        f"{project_service.display_name(project)}'s fleet is not down — {what}. Nothing was "
        f"removed and the board was not cleared: take it down with {shutdown}, then start "
        "fresh again"
    )
    if stopped:
        report.sessions_failed = list(dict.fromkeys([*report.sessions_failed, *(standing or [])]))
        raise FreshIncomplete(said, report)
    raise FreshRefused(f"can't start fresh: {said}", code="fleet_error")


def _held_now(
    project: ProjectInfo, report: FreshReport, *, branches: bool
) -> tuple[list[FreshUnpushed], list[Path], dict[str, str]]:
    """What the project's trees and (with ``branches``) its fleet branches hold that no
    remote has, read again once its fleet is stopped, when nothing runs there to add to it:
    the work found, the trees, and the branches' tips. Read from the rows as they are now,
    so a tree an agent started since the plan is asked about too.

    A row live again is :class:`FreshIncomplete`, before any tree is touched: an agent a
    spawn started after the stop (a respawn, a hand ``fleet spawn``) runs in its tree, which
    was removed under it, forced when anything was discarded, before the store's own
    live-row refusal (review of #429, round 2, n1). The store's check stays the last word."""
    with store_session() as store:
        rows = store.fleet_agents(project.id)
    live = [row.label for row in rows if row.ended_at is None]
    if live:
        raise FreshIncomplete(
            f"{len(live)} fleet agent(s) started since its fleet was stopped: "
            f"{', '.join(live)} — nothing was removed and the board was not cleared. Stop "
            f"them ({project_service.fleet_command('shutdown', project)}), then start fresh "
            "again",
            report,
        )
    trees = fleet_service.fleet_trees(project, rows)
    tips = fleet_service.fleet_branch_tips(project, rows) if branches else {}
    return fleet_service.unpushed_work(project, trees, tips), trees, tips


def _uncovered(found: Sequence[FreshUnpushed], discard: Discard) -> list[FreshUnpushed]:
    """What of ``found`` ``discard`` does not let go: everything when it is empty, nothing
    when it is ``"all"``, else each item it does not name at the same tree, branch and tip
    (no commit made since it was shown), and each with uncommitted changes where the one it
    named had none. A blanket yes to a list someone saw threw away work written after they
    saw it (review of #429, M1). Asked before the stop and after it, alike.

    Uncommitted changes are confirmed as a yes or a no, not as their contents: a tree listed
    with uncommitted changes goes with them as they are when the reset runs, edits made
    since at the same tip included. The dialog and the CLI's prompt say so (review of #429,
    round 2)."""
    if discard == "all":
        return []

    def place(item: FreshUnpushed) -> tuple[Path | None, str | None, str | None]:
        return item.path, item.branch, item.tip

    allowed = {place(item): item for item in discard}
    return [
        item
        for item in found
        if (shown := allowed.get(place(item))) is None
        or (item.uncommitted and not shown.uncommitted)
    ]


def count_words(counts: FreshCounts) -> list[str]:
    """``3 tasks``, ``41 notes and events``, …: what ``counts`` holds, the extras chosen too,
    as ``project fresh`` and the UI's Start fresh… say it."""
    words = [
        (counts.tasks, "task", "tasks"),
        (counts.events, "note or event", "notes and events"),
        (counts.sessions, "session", "sessions"),
        (counts.meta, "signal or mark", "signals and marks"),
        (counts.agents, "fleet agent", "fleet agents"),
        (counts.worktrees, "worktree", "worktrees"),
        (counts.entries, "context entry", "context entries"),
        (counts.prompts, "prompt", "prompts"),
        (counts.metrics, "turn metric", "turn metrics"),
        (counts.branches, "fleet branch", "fleet branches"),
    ]
    return [f"{n} {one if n == 1 else many}" for n, one, many in words if n is not None]


def cleared_words(report: FreshReport) -> list[str]:
    """:func:`count_words` of what ``report`` cleared, its events with how many of them were
    written during the reset (``FreshReport.stop_events``: the stop's own, and what an agent
    posted before it was stopped): the plan counted the rest, so a plan of 7 and a run that
    cleared 11 read ``11 notes and events (4 written during the reset)``."""
    words = count_words(report.cleared)
    if extra := report.stop_events():
        words[1] += f" ({extra} written during the reset)"  # [1]: the events, never None
    return words


def unpushed_brief(item: FreshUnpushed) -> str:
    """``fleet/x/coder-1: 2 commits on no remote, uncommitted changes``: :func:`unpushed_line`
    named by its branch, or its tree's folder when detached, without the path."""
    name = item.branch or (item.path.name if item.path is not None else "?")
    return f"{name}: {_held_words(item)}"


def unpushed_line(item: FreshUnpushed) -> str:
    """``<tree> on <branch>: <n> commit(s) on no remote, uncommitted changes``, or
    ``branch <name>: …`` for a branch no tree has out."""
    if item.path is None:
        where = f"branch {item.branch}"
    else:
        where = f"{item.path} on {item.branch}" if item.branch else f"{item.path} (detached)"
    return f"{where}: {_held_words(item)}"


def _held_words(item: FreshUnpushed) -> str:
    """``2 commits on no remote, uncommitted changes``: what ``item`` holds, from its fields
    (a path may hold ": ", so its line is never parsed back)."""
    what = []
    if item.commits:
        what.append(f"{item.commits} commit{'s' if item.commits != 1 else ''} on no remote")
    if item.uncommitted:
        what.append("uncommitted changes")
    return ", ".join(what)


DISCARD_FLAG = "pass --discard-unpushed to throw it away"
"""How ``project fresh`` lets work found nowhere else go: what its refusal says to do."""

RUN_AGAIN = "run it again to see and confirm the new list"
"""What a refusal found once the fleet was stopped tells someone who confirmed a list at a
terminal: the flag they passed, named again, would only ask them about the same list."""


def found_after_stop(
    planned: FreshPlan, *, way: str = DISCARD_FLAG, limit: int | None = None, brief: bool = False
) -> str:
    """The refusal of work found once the fleet was stopped, which the confirmed discard does
    not cover (:class:`FreshIncomplete` with ``plan``): work committed during the stop
    itself, the rest being refused before it. With ``way``, ``limit`` and ``brief`` as
    :func:`unpushed_refusal` takes them, then that the fleet is stopped and how to start it
    again: refused there, the project is left with nothing running."""
    restart = project_service.fleet_command("restart", planned.project) + " <label>"
    spawn = project_service.fleet_command("spawn", planned.project, "manager")
    refusal = unpushed_refusal(planned, way=way, limit=limit, brief=brief)
    return (
        f"{refusal} — found once its fleet was "
        "stopped: nothing was removed and the board was not cleared, but its agents are "
        f"stopped. Start them again with {restart} (the manager first), or a new manager "
        f"with {spawn}"
    )


def unpushed_refusal(
    planned: FreshPlan, *, way: str = DISCARD_FLAG, limit: int | None = None, brief: bool = False
) -> str:
    """The refusal of a plan whose trees or branches hold work found nowhere else, with
    ``way``, how the surface asking lets it go: the CLI's flag by default; the UI says to
    open Start fresh… again and confirm the discard. ``limit`` names at most that many
    places and counts the rest, and ``brief`` names each by its branch (or its tree's folder,
    detached) without the tree's path, which the dialog lists: a toast at 80x24 listing
    several long paths was taller than the screen, and its title and what to do scrolled off
    (tester-fresh-ui)."""
    items = planned.unpushed if limit is None else planned.unpushed[:limit]
    lines = "; ".join((unpushed_brief if brief else unpushed_line)(item) for item in items)
    if more := len(planned.unpushed) - len(items):
        lines += f"; …and {more} more"
    return (
        f"can't start fresh: {project_service.display_name(planned.project)}'s fleet holds "
        f"work found nowhere else — push it, or {way}: {lines}"
    )


def _refuse(store: ContextStore, project: ProjectInfo) -> ProjectInfo:
    """``project`` as the store has it, unless :func:`plan` refuses it (:class:`FreshRefused`)."""
    stored = store.get_project(project.id)
    if stored is None or stored.onboarded_at is None:
        raise FreshRefused(str(project_service.NotAdded(project)), code="not_onboarded")
    if is_the_home(stored.root):
        raise FreshRefused(
            "can't start fresh in the aisquare home: it is the captain's board, not a project",
            code="the_home",
        )
    own = orchestrator.env_fleet_agent()
    row = store.get_fleet_agent(own) if own else None
    if row is not None and row.project_id == stored.id:
        raise FreshRefused(
            f"can't start fresh from inside {row.label}, one of "
            f"{project_service.display_name(stored)}'s own fleet agents: the stop would take "
            "this pane down part-way through. Run it from the UI (asq) or from a terminal of "
            "your own",
            code="inside_fleet_agent",
        )
    readers = project_service.board_readers(store, stored)
    if readers:
        busy = project_service.ProjectBusyError(stored, readers=readers)
        raise FreshRefused(f"can't start fresh: {busy}", code="project_busy")
    return stored


def _sessions(project: ProjectInfo, *, live: bool) -> list[str]:
    """``<socket>:<session>`` for each of the project's tmux sessions the stop kills, as
    ``fleet shutdown``'s plan lists them, refusing as it refuses (:class:`FleetError`).

    Asked of the plan only when there is something to stop: a live row, or a session of
    the project's standing (:func:`fleet_service.project_sessions`, read without the plan's
    guards). The plan refuses any caller inside the fleet's own tmux server, and asked
    always, it refused a project with nothing to stop from another project's pane (review
    of #429, m1). With something to stop it is refused there still, in this command's words:
    the stop kills sessions on the server the caller runs in. With no live row and no usable
    tmux here, none: nothing could be killed, and nothing is said to run.

    A session that could not be asked (``project_sessions`` says ``None``) refuses the reset
    with nothing touched. Read as absent, the stop was skipped (review of #429, round 2,
    n2); handed to the plan instead, which asks a session no live row lived on only just in
    case and passes over one it cannot ask, the stop was skipped all the same, and the tree
    of a process still running in that session was removed (tester-fresh-fix at c85beaa4).
    ``fleet shutdown``'s own plan stays as lenient as it is."""
    standing = fleet_service.project_sessions(project)
    if standing is None:
        session = fleet_service.session_name(project.codename or "")
        raise FreshRefused(
            f"can't start fresh: tmux could not say whether {session} stands, so its "
            f"agents could not be known stopped — nothing was touched; try again once tmux "
            "answers",
            code="fleet_error",
        )
    if not live and not standing:
        return []
    try:
        # With the strict read's sessions too: the plan asks one no live row lived on only
        # just in case, and passes over a failure to ask it (review of #429, round 4).
        return list(dict.fromkeys([*standing, *fleet_service.shutdown_plan(project).sessions]))
    except fleet_service.InsideTheFleet as exc:
        raise FreshRefused(
            f"can't start fresh from inside the fleet's own tmux server: stopping "
            f"{project_service.display_name(project)}'s fleet kills sessions on the server "
            "this pane runs in, before anything could be reported. Run it from the UI (asq) "
            "or from a terminal outside the fleet",
            code="inside_fleet",
        ) from exc
    except fleet_service.FleetUnavailable:
        if live:
            raise
        return []


def _stopped(report: fleet_service.ShutdownReport) -> list[str]:
    """The labels a shutdown ended: stopped, or recorded gone on a server that was."""
    return [agent.label for agent in report.stopped] + [row.agent.label for row in report.recorded]


def _counts(tables: Mapping[str, int], *, worktrees: int, branches: int | None) -> FreshCounts:
    """``store.reset_project``'s per-table counts, by what a person calls them."""
    return FreshCounts(
        tasks=tables.get("team_task", 0),
        events=tables.get("team_event", 0),
        sessions=tables.get("team_session", 0),
        meta=tables.get("team_meta", 0),
        agents=tables.get("fleet_agent", 0),
        worktrees=worktrees,
        entries=tables.get("entry"),
        prompts=tables.get("prompt"),
        metrics=tables.get("metric"),
        branches=branches,
    )
