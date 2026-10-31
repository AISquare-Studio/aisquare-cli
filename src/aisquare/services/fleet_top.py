"""What each live agent consumes: ``aisquare fleet top`` and asq's *Agent resources* page.

Per live fleet row: the CPU, the resident memory and the number of processes of the tree its
tmux pane started (``claude`` and everything under it: MCP servers, a ``pytest``, a build),
how long it has run, and what its session has spent so far in tokens and at list prices.

**One read of each thing per sample**, whatever the number of agents: the store's live rows
(one query), one ``list-panes -a`` per tmux server the rows live on (one in practice, the
fleet's ``asq``), ONE ``ps -A`` for every process (``core.process_table``), and each session's
spend (:class:`SpendReader`). Nothing here writes: no row is ended, no pane or process is
touched — a row whose pane is gone reads ``gone`` until ``fleet ls`` or ``reap`` records it.

**Spend** starts from what the session's Stop hooks recorded (``usage_session``: the totals
and where their scan stopped, ``services.metrics``) and scans only what the transcript gained
since, so it is current mid-turn and costs a few milliseconds a session. A session no hook
recorded is scanned from its first line, once: a :class:`SpendReader` kept between samples
(the page keeps one) reads only the new bytes after that.
"""

from __future__ import annotations

import contextlib
import functools
import os
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from aisquare.core import process_table
from aisquare.core import usage as usage_core
from aisquare.core.store import ContextStore, store_session
from aisquare.core.tmux import TmuxError, TmuxServer
from aisquare.models import FleetAgent
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import fleet as fleet_service
from aisquare.services import project as project_service

State = Literal["running", "exited", "gone", "unknown"]
"""An agent's process tree as one sample saw it:

- ``running``: its pane's process was in the process table;
- ``exited``: its pane is dead (``remain-on-exit``), or its process was not in the table
  (it exited between the tmux read and the ``ps`` read);
- ``gone``: its server answered and holds no such pane;
- ``unknown``: its server did not answer, or ``ps`` could not be read."""

SortKey = Literal["cpu", "mem", "tokens"]
SORT_KEYS: tuple[SortKey, ...] = ("cpu", "mem", "tokens")


@dataclass(frozen=True)
class Spend:
    """What a session has spent so far."""

    tokens: int
    """Every token billed: input, cache writes, cache reads and output."""
    cost_usd: float
    """At list prices (``core.pricing``); a model with no price adds nothing (``priced``)."""
    priced: bool


class SpendReader:
    """Each session's spend, kept between samples so that each scans only what its transcript
    gained since the one before (the page's tick every few seconds).

    The first read of a session starts from its recorded ``usage_session`` row when it has one
    (its totals and its scan's position), else from the transcript's first line."""

    def __init__(self) -> None:
        self._seen: dict[str, tuple[Path, usage_core.Usage, usage_core.ScanState]] = {}

    def spend(self, store: ContextStore, session_id: str) -> Spend | None:
        """``session_id``'s spend now, or ``None`` when nothing of it can be read (no transcript
        recorded, none on disk, no usage row)."""
        seen = self._seen.get(session_id)
        if seen is None:
            seen = self._start(store, session_id)
            if seen is None:
                return None
        transcript, total, state = seen
        if transcript.is_file():
            delta, state = usage_core.scan(transcript, state)
            total.add(delta)
        self._seen[session_id] = (transcript, total, state)
        totals = total.totals()
        return Spend(totals.total, totals.cost_usd, totals.priced)

    @staticmethod
    def _start(
        store: ContextStore, session_id: str
    ) -> tuple[Path, usage_core.Usage, usage_core.ScanState] | None:
        recorded = store.session_usage(session_id)
        path = recorded.transcript_path if recorded is not None else None
        if not path:
            session = store.get_session(session_id)
            path = session.transcript_path if session is not None else None
        if not path:
            return None
        if recorded is None:
            return Path(path), usage_core.Usage(), usage_core.ScanState()
        return (
            Path(path),
            usage_core.Usage.from_dict(recorded.usage),
            usage_core.ScanState.parse(recorded.state),
        )

    def forget(self, keep: Collection[str]) -> None:
        """Drop every session but ``keep``'s: an agent that ended takes its memory with it."""
        for session_id in list(self._seen):
            if session_id not in keep:
                del self._seen[session_id]


@dataclass(frozen=True)
class AgentUse:
    """One live agent in a sample."""

    agent_id: str
    project_id: str
    project: str
    """The project as a person names it (``services.project.display_name``)."""
    codename: str | None
    label: str
    role: str
    account: str | None
    """The account's label (its alias, ``plain claude``, ``account N``); ``None``: none chosen."""
    account_slot: int | None
    session_id: str | None
    pane_id: str
    state: State
    pid: int | None = None
    """The process the pane started: the agent's own (``aisquare launch`` execs it)."""
    cpu: float | None = None
    """Percent of one core, the tree summed (``process_table``: ``ps``'s ``pcpu``)."""
    rss_kb: int | None = None
    processes: int = 0
    uptime: int | None = None
    """Seconds the pane's process has run."""
    tokens: int | None = None
    cost_usd: float | None = None
    priced: bool = True

    def as_json(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "project_id": self.project_id,
            "project": self.project,
            "codename": self.codename,
            "label": self.label,
            "role": self.role,
            "account": self.account,
            "account_slot": self.account_slot,
            "session_id": self.session_id,
            "pane_id": self.pane_id,
            "state": self.state,
            "pid": self.pid,
            "cpu_percent": self.cpu,
            "rss_kb": self.rss_kb,
            "processes": self.processes,
            "uptime_seconds": self.uptime,
            "tokens": self.tokens,
            "cost_usd": None if self.cost_usd is None else round(self.cost_usd, 4),
            "priced": self.priced,
        }


@dataclass(frozen=True)
class Totals:
    """Every agent of a sample summed: the totals row."""

    agents: int
    running: int
    cpu: float
    rss_kb: int
    processes: int
    tokens: int
    cost_usd: float
    priced: bool

    def as_json(self) -> dict[str, Any]:
        return {
            "agents": self.agents,
            "running": self.running,
            "cpu_percent": self.cpu,
            "rss_kb": self.rss_kb,
            "processes": self.processes,
            "tokens": self.tokens,
            "cost_usd": round(self.cost_usd, 4),
            "priced": self.priced,
        }


@dataclass(frozen=True)
class TopReport:
    """One sample of every live agent (:func:`sample`)."""

    agents: list[AgentUse]
    sampled_at: datetime
    took_ms: float
    """How long the sample took, store, tmux, ``ps`` and transcripts together."""
    ps_read: bool = True
    """Whether ``ps`` answered; when it did not, every agent is ``unknown``."""
    unanswered: list[str] = field(default_factory=list)
    """The tmux servers that did not answer, by socket: their agents are ``unknown``."""

    def totals(self) -> Totals:
        running = [agent for agent in self.agents if agent.state == "running"]
        return Totals(
            agents=len(self.agents),
            running=len(running),
            cpu=round(sum(agent.cpu or 0.0 for agent in running), 1),
            rss_kb=sum(agent.rss_kb or 0 for agent in running),
            processes=sum(agent.processes for agent in running),
            tokens=sum(agent.tokens or 0 for agent in self.agents),
            cost_usd=sum(agent.cost_usd or 0.0 for agent in self.agents),
            priced=all(agent.priced for agent in self.agents),
        )

    def sorted(self, by: SortKey = "cpu") -> list[AgentUse]:
        return sort_agents(self.agents, by)

    def as_json(self, by: SortKey = "cpu") -> dict[str, Any]:
        return {
            "sampled_at": self.sampled_at.isoformat(),
            "took_ms": round(self.took_ms, 1),
            "sort": by,
            "ps_read": self.ps_read,
            "unanswered": list(self.unanswered),
            "agents": [agent.as_json() for agent in self.sorted(by)],
            "totals": self.totals().as_json(),
        }


def sort_agents(agents: Collection[AgentUse], by: SortKey) -> list[AgentUse]:
    """Heaviest first by ``by`` (CPU, resident memory or tokens); an agent with no figure for
    it last; ties by project, then label, so a refresh does not shuffle equal rows."""

    def figure(agent: AgentUse) -> float:
        value = {"cpu": agent.cpu, "mem": agent.rss_kb, "tokens": agent.tokens}[by]
        return -1.0 if value is None else float(value)

    return sorted(agents, key=lambda agent: (-figure(agent), agent.project, agent.label))


ServerOf = Callable[[FleetAgent], TmuxServer]


def sample(
    project_ids: Collection[str] | None = None,
    *,
    spend: SpendReader | None = None,
    read_ps: process_table.Reader | None = None,
    server_of: ServerOf | None = None,
) -> TopReport:
    """Every live agent's resources now: of ``project_ids``' fleets, or every project's.

    ``spend`` keeps each session's scan between samples (a fresh one otherwise); ``read_ps``
    and ``server_of`` are the ``ps`` read and the tmux server of a row, which a test fakes.
    """
    began = time.perf_counter()
    spend = spend if spend is not None else SpendReader()
    with store_session() as store:
        rows = [
            row
            for row in store.live_fleet_agents()
            if project_ids is None or row.project_id in project_ids
        ]
        projects = {row.project_id: store.get_project(row.project_id) for row in rows}
        labels = accounts_service.slot_labels(store) if any(r.account_slot for r in rows) else {}
    if not rows:  # nothing to read the processes of: neither tmux nor ps is asked
        spend.forget(())
        return TopReport([], datetime.now(tz=UTC), (time.perf_counter() - began) * 1000)
    if server_of is None:
        config = fleet_service.settings()  # read once, not once a row
        server_of = functools.partial(fleet_service.server_of, config=config)
    panes, unanswered = _pane_pids(rows, server_of)
    table = process_table.read(read_ps)
    me = os.getpid()
    agents: list[AgentUse] = []
    with store_session() as store:
        for row in rows:
            project = projects.get(row.project_id)
            pids = panes.get(row.id)
            agents.append(
                _agent_use(
                    row,
                    project_service.display_name(project) if project else row.project_id,
                    project.codename if project else None,
                    _account(row.account_slot, labels),
                    pids,
                    table,
                    me,
                    spend.spend(store, row.session_id) if row.session_id else None,
                )
            )
    spend.forget({row.session_id for row in rows if row.session_id})
    return TopReport(
        agents=agents,
        sampled_at=datetime.now(tz=UTC),
        took_ms=(time.perf_counter() - began) * 1000,
        ps_read=table is not None,
        unanswered=unanswered,
    )


_Panes = dict[str, int | None] | None
"""A row's server's answer: pane id → its pid (``None`` for a dead pane), or ``None`` when the
server did not answer."""


def _pane_pids(rows: list[FleetAgent], locate: ServerOf) -> tuple[dict[str, _Panes], list[str]]:
    """Row id → its server's panes, each server asked once; and the servers that did not
    answer, by socket."""
    asked: dict[tuple[str, str | None], _Panes] = {}
    by_row: dict[str, _Panes] = {}
    unanswered: list[str] = []
    for row in rows:
        try:
            server = locate(row)
        except Exception:  # no tmux at all, an unreadable config: this row is unknown
            by_row[row.id] = None
            continue
        key = (server.socket, str(server.path) if server.path is not None else None)
        if key not in asked:
            answer: _Panes = None
            with contextlib.suppress(TmuxError):
                answer = server.pane_pids()
            asked[key] = answer
            if answer is None:
                unanswered.append(key[1] or key[0])
        by_row[row.id] = asked[key]
    return by_row, unanswered


def _account(slot: int | None, labels: dict[int, str]) -> str | None:
    if slot is None:
        return None
    return labels.get(slot) or ("plain claude" if slot == 1 else f"account {slot}")


def _agent_use(
    row: FleetAgent,
    project: str,
    codename: str | None,
    account: str | None,
    panes: _Panes,
    table: process_table.Table | None,
    me: int,
    spent: Spend | None,
) -> AgentUse:
    base = AgentUse(
        agent_id=row.id,
        project_id=row.project_id,
        project=project,
        codename=codename,
        label=row.label,
        role=row.role,
        account=account,
        account_slot=row.account_slot,
        session_id=row.session_id,
        pane_id=row.pane_id,
        state="unknown",
        tokens=spent.tokens if spent else None,
        cost_usd=spent.cost_usd if spent else None,
        priced=spent.priced if spent else True,
    )
    if panes is None or table is None:
        return base
    if row.pane_id not in panes:
        return replace(base, state="gone")
    pid = panes[row.pane_id]
    tree = table.tree(pid, skip=me) if pid is not None else None
    if tree is None:
        return replace(base, state="exited", pid=pid)
    return replace(
        base,
        state="running",
        pid=pid,
        cpu=tree.cpu,
        rss_kb=tree.rss_kb,
        processes=tree.processes,
        uptime=tree.uptime,
    )
