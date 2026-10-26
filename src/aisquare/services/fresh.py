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

from collections.abc import Mapping

from aisquare.core import orchestrator
from aisquare.core.store import ContextStore, is_the_home, store_session
from aisquare.models import FreshCounts, FreshPlan, FreshReport, FreshUnpushed, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import project as project_service


class FreshRefused(Exception):
    """Start fresh will not run, and nothing was touched. ``code`` says why, for ``--json``:
    ``not_onboarded``, ``the_home``, ``inside_fleet_agent``, ``project_busy`` (another
    project's fleet reads its board), ``fleet_error`` (the fleet or git could not be asked),
    ``unpushed_work`` (with the ``plan`` that found it)."""

    def __init__(self, message: str, *, code: str, plan: FreshPlan | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.plan = plan


class FreshIncomplete(Exception):
    """Start fresh stopped part-way, and nothing on the board was cleared: an agent stood
    after the stop, or one was started since. ``report`` says what was done before it."""

    def __init__(self, message: str, report: FreshReport) -> None:
        super().__init__(message)
        self.report = report


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
    here (``fleet shutdown``'s own refusals: no usable tmux, a call from inside its server)."""
    with store_session() as store:
        project = _refuse(store, project)
        rows = store.fleet_agents(project.id)
        tables = store.reset_project(project.id, memory=memory, prompts=prompts, apply=False)
    live = sorted(
        (row for row in rows if row.ended_at is None),
        key=lambda row: row.label != fleet_service.MANAGER_LABEL,
    )
    try:
        if live:
            fleet_service.shutdown_plan(project)  # its refusals, before anything is touched
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
    discard_unpushed: bool = False,
    actor: str = "user",
    ack_wait: float = 0.0,
) -> FreshReport:
    """Clear ``project``'s board and fleet, and start it a fresh manager.

    Planned again first (:func:`plan`, whose refusals are this one's), and refused while
    any tree or branch it would remove holds work found nowhere else, unless
    ``discard_unpushed``. Then, in this order, so that a step that fails leaves what a run
    again can finish: the live agents stopped (``fleet shutdown``'s path: claims released,
    ``agent_exited`` said, the project's tmux session killed), the agents' worktrees removed
    (``--force`` only when discarding), with ``branches`` the fleet branches deleted, the
    rows cleared in one transaction (``store.reset_project``), and a manager spawned as
    ``fleet spawn manager`` spawns one, a new session id and the only one on the board.

    :class:`FreshIncomplete` when an agent still stands after the stop, or a spawn started
    one before the rows went: nothing on the board is cleared then. A manager the fleet
    refuses to start is said on the report (``manager_error``), the board clear all the same.
    ``actor`` is who asked, on each ``agent_exited``; ``ack_wait`` is the spawn's.
    """
    planned = plan(project, memory=memory, prompts=prompts, branches=branches)
    if planned.unpushed and not discard_unpushed:
        raise FreshRefused(unpushed_refusal(planned), code="unpushed_work", plan=planned)
    project = planned.project
    report = FreshReport(project=project, discarded=planned.unpushed)
    if planned.live_agents:
        try:
            down = fleet_service.shutdown(project, actor=actor)
        except fleet_service.FleetInterrupted as exc:
            report.stopped = _stopped(exc.report)
            raise FreshIncomplete(
                f"interrupted while stopping {project_service.display_name(project)}'s "
                "fleet — nothing on the board was cleared",
                report,
            ) from None
        except fleet_service.FleetError as exc:
            raise FreshRefused(f"can't start fresh: {exc}", code="fleet_error") from exc
        report.stopped = _stopped(down)
        if down.failed:
            standing = "; ".join(f"{row.agent.label}: {row.reason}" for row in down.failed)
            raise FreshIncomplete(
                f"{len(down.failed)} agent(s) were left live, so nothing on the board was "
                f"cleared — {standing}. Once they are down, start fresh again",
                report,
            )
    report.worktrees_removed, kept = fleet_service.remove_fleet_trees(
        project, planned.worktrees, force=discard_unpushed
    )
    report.kept += kept
    if branches:
        report.branches_deleted, kept = fleet_service.delete_fleet_branches(
            project, planned.fleet_branches
        )
        report.kept += kept
    with store_session() as store:
        try:
            tables = store.reset_project(project.id, memory=memory, prompts=prompts)
        except ValueError as exc:
            raise FreshIncomplete(
                f"{exc}, started since its fleet was stopped — nothing on the board was "
                "cleared. Stop it, then start fresh again",
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
    else:
        report.manager = receipt.agent
    return report


def unpushed_line(item: FreshUnpushed) -> str:
    """``<tree> on <branch>: <n> commit(s) on no remote, uncommitted changes``, or
    ``branch <name>: …`` for a branch no tree has out."""
    if item.path is None:
        where = f"branch {item.branch}"
    else:
        where = f"{item.path} on {item.branch}" if item.branch else f"{item.path} (detached)"
    what = []
    if item.commits:
        what.append(f"{item.commits} commit{'s' if item.commits != 1 else ''} on no remote")
    if item.uncommitted:
        what.append("uncommitted changes")
    return f"{where}: {', '.join(what)}"


def unpushed_refusal(planned: FreshPlan) -> str:
    """The refusal of a plan whose trees or branches hold work found nowhere else."""
    lines = "; ".join(unpushed_line(item) for item in planned.unpushed)
    return (
        f"can't start fresh: {project_service.display_name(planned.project)}'s fleet holds "
        f"work found nowhere else — push it, or pass --discard-unpushed to throw it away: "
        f"{lines}"
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
