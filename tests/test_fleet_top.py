"""``aisquare fleet top`` and ``services.fleet_top``: what each live agent consumes.

Driven over real store rows with ``ps`` and tmux faked: ``ps`` is text in the shape it prints,
and the tmux server answers ``list-panes -a`` from a dict (its parsing is the real one). The
sessions' spend is read off real transcripts and a recorded ``usage_session`` row.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import usage as usage_core
from aisquare.core.store import store_session
from aisquare.core.tmux import Completed, TmuxServer
from aisquare.models import FleetAgent, ProjectInfo, SessionUsage, TeamSession
from aisquare.services import fleet_top
from tests.projects import add_project
from tests.usage_stats_support import request, write

runner = CliRunner()
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)

#: Two agents and two strangers. ``%1``'s claude (500) runs an MCP server (501) and a shell
#: (502) running pytest (503); ``%2``'s claude (600) runs alone.
PS = """\
    1     0   4.0  20000  2-00:00:00
  500     1  12.5 512000     1:02:03
  501   500   1.5  80000       05:09
  502   500   0.0   4000       04:00
  503   502  85.0 300000       00:40
  600     1   2.0 400000       30:00
  900     1   3.0  10000       10:00
"""


class ListedPanes(TmuxServer):
    """A fleet server that answers ``list-panes -a`` from ``panes`` (pane id → pid, ``None``
    for a dead pane) and runs no tmux. ``panes=None``: it does not answer; ``absent``: no
    server runs behind the socket."""

    def __init__(self, panes: dict[str, int | None] | None, *, absent: bool = False) -> None:
        super().__init__("asq", runner=self._answer)
        self.panes = panes
        self.absent = absent
        self.asked = 0

    def argv(self, *args: str) -> list[str]:
        return ["tmux", "-L", self.socket, *args]

    def _answer(self, argv: Sequence[str], stdin: bytes | None) -> Completed:
        assert list(argv[3:6]) == ["list-panes", "-a", "-F"], argv
        self.asked += 1
        if self.absent:
            return Completed(1, "", "no server running on /tmp/tmux-501/asq\n")
        if self.panes is None:
            return Completed(1, "", "server exited unexpectedly\n")
        lines = [
            f"{pane}|~|{'' if pid is None else pid}|~|{'1' if pid is None else '0'}"
            for pane, pid in self.panes.items()
        ]
        return Completed(0, "\n".join(lines) + "\n", "")


class Ps:
    """``ps`` as text, counted: every sample must read it exactly once."""

    def __init__(self, text: str | None = PS) -> None:
        self.text = text
        self.reads = 0

    def __call__(self) -> str | None:
        self.reads += 1
        return self.text


@pytest.fixture
def project(tmp_path: Path) -> ProjectInfo:
    root = tmp_path / "shop"
    root.mkdir()
    return add_project(root)


def _agent(
    project: ProjectInfo,
    label: str,
    pane: str,
    *,
    role: str = "coder",
    session: str | None = None,
    slot: int | None = None,
    ended: bool = False,
) -> FleetAgent:
    row = FleetAgent(
        id=f"agt_{label}",
        project_id=project.id,
        label=label,
        role=role,
        pane_id=pane,
        session_id=session,
        cwd=project.root,
        created_at=NOW,
        account_slot=slot,
        ended_at=NOW if ended else None,
    )
    with store_session() as store:
        store.upsert_fleet_agent(row)
    return row


def _sample(
    server: TmuxServer, ps: Ps, project_ids: set[str] | None = None, **kwargs: Any
) -> fleet_top.TopReport:
    return fleet_top.sample(project_ids, read_ps=ps, server_of=lambda row: server, **kwargs)


def _by_label(report: fleet_top.TopReport) -> dict[str, fleet_top.AgentUse]:
    return {agent.label: agent for agent in report.agents}


# --- the tree of each pane ------------------------------------------------------------------


def test_each_agent_is_its_panes_tree_summed_from_one_ps_and_one_tmux_listing(
    project: ProjectInfo,
) -> None:
    _agent(project, "coder-1", "%1", slot=1)
    _agent(project, "manager", "%2", role="manager")
    server, ps = ListedPanes({"%1": 500, "%2": 600, "%7": 900}), Ps()

    report = _sample(server, ps)

    assert (ps.reads, server.asked) == (1, 1), "one ps and one list-panes for every agent"
    rows = _by_label(report)
    coder = rows["coder-1"]
    assert (coder.state, coder.pid, coder.cpu, coder.rss_kb, coder.processes, coder.uptime) == (
        "running",
        500,
        99.0,
        896_000,
        4,
        3723,
    )
    assert (coder.project, coder.role, coder.account) == ("shop", "coder", "plain claude")
    manager = rows["manager"]
    assert (manager.cpu, manager.rss_kb, manager.processes, manager.uptime) == (
        2.0,
        400_000,
        1,
        1800,
    )
    assert manager.account is None
    totals = report.totals()
    assert (totals.agents, totals.running, totals.cpu, totals.rss_kb, totals.processes) == (
        2,
        2,
        101.0,
        1_296_000,
        5,
    )
    assert report.ps_read and report.unanswered == []


def test_a_child_that_exits_between_two_samples_is_gone_from_the_next(
    project: ProjectInfo,
) -> None:
    _agent(project, "coder-1", "%1")
    server = ListedPanes({"%1": 500})

    first = _by_label(_sample(server, Ps()))["coder-1"]
    without_pytest = PS.replace("  503   502  85.0 300000       00:40\n", "")
    second = _by_label(_sample(server, Ps(without_pytest)))["coder-1"]

    assert (first.processes, first.cpu) == (4, 99.0)
    assert (second.state, second.processes, second.cpu, second.rss_kb) == (
        "running",
        3,
        14.0,
        596_000,
    )


def test_a_child_whose_parent_exited_before_the_read_hangs_under_init_not_the_agent(
    project: ProjectInfo,
) -> None:
    """The shell (502) exited between ``ps`` lines being produced: its pytest was reparented
    to launchd, and the one snapshot says so — the agent is not charged for it."""
    _agent(project, "coder-1", "%1")
    orphaned = PS.replace("  502   500   0.0   4000       04:00\n", "").replace(
        "  503   502", "  503     1"
    )

    coder = _by_label(_sample(ListedPanes({"%1": 500}), Ps(orphaned)))["coder-1"]

    assert (coder.processes, coder.cpu) == (2, 14.0)


def test_an_agent_whose_process_exited_between_the_tmux_read_and_ps_reads_exited(
    project: ProjectInfo,
) -> None:
    _agent(project, "coder-1", "%1")

    coder = _by_label(_sample(ListedPanes({"%1": 777}), Ps()))["coder-1"]

    assert (coder.state, coder.pid, coder.cpu, coder.processes) == ("exited", 777, None, 0)


def test_an_agent_whose_pane_is_gone_is_listed_gone_and_adds_nothing(
    project: ProjectInfo,
) -> None:
    _agent(project, "coder-1", "%1")
    _agent(project, "coder-2", "%9")

    report = _sample(ListedPanes({"%1": 500}), Ps())

    gone = _by_label(report)["coder-2"]
    assert (gone.state, gone.pid, gone.cpu, gone.rss_kb, gone.processes) == (
        "gone",
        None,
        None,
        None,
        0,
    )
    totals = report.totals()
    assert (totals.agents, totals.running, totals.processes) == (2, 1, 4)
    with store_session() as store:  # read-only: the row is not ended by a look
        assert {row.label for row in store.live_fleet_agents()} == {"coder-1", "coder-2"}


def test_a_dead_pane_kept_by_remain_on_exit_is_exited(project: ProjectInfo) -> None:
    _agent(project, "coder-1", "%1")

    coder = _by_label(_sample(ListedPanes({"%1": None}), Ps()))["coder-1"]

    assert (coder.state, coder.pid) == ("exited", None)


def test_a_server_that_does_not_answer_makes_its_agents_unknown_not_gone(
    project: ProjectInfo,
) -> None:
    _agent(project, "coder-1", "%1")

    report = _sample(ListedPanes(None), Ps())

    assert _by_label(report)["coder-1"].state == "unknown"
    assert report.unanswered == ["asq"]


def test_no_server_behind_the_socket_means_every_pane_is_gone(project: ProjectInfo) -> None:
    _agent(project, "coder-1", "%1")

    report = _sample(ListedPanes({}, absent=True), Ps())

    assert _by_label(report)["coder-1"].state == "gone" and report.unanswered == []


def test_with_ps_unreadable_every_agent_is_unknown_and_the_report_says_so(
    project: ProjectInfo,
) -> None:
    _agent(project, "coder-1", "%1")

    report = _sample(ListedPanes({"%1": 500}), Ps(None))

    assert _by_label(report)["coder-1"].state == "unknown"
    assert not report.ps_read


def test_the_samplers_own_process_is_not_charged_to_the_agent_it_runs_under(
    project: ProjectInfo,
) -> None:
    """An agent's shell runs ``fleet top``: the aisquare (this process) and its ps are left out."""
    _agent(project, "coder-1", "%1")
    me = os.getpid()
    mine = PS + f"{me:>5}   502  60.0 90000       00:01\n{me + 1:>5} {me:>5}   5.0  1000  00:00\n"

    coder = _by_label(_sample(ListedPanes({"%1": 500}), Ps(mine)))["coder-1"]

    assert (coder.processes, coder.cpu) == (4, 99.0)


def test_only_live_rows_of_the_projects_asked_for_are_read(
    project: ProjectInfo, tmp_path: Path
) -> None:
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = add_project(other_root)
    _agent(project, "coder-1", "%1")
    _agent(project, "coder-old", "%3", ended=True)
    _agent(other, "elsewhere", "%2")
    server = ListedPanes({"%1": 500, "%2": 600})

    everyone = {a.label for a in _sample(server, Ps()).agents}
    mine = {a.label for a in _sample(server, Ps(), {project.id}).agents}

    assert everyone == {"coder-1", "elsewhere"}
    assert mine == {"coder-1"}


def test_with_no_live_agent_neither_tmux_nor_ps_is_asked(project: ProjectInfo) -> None:
    _agent(project, "coder-old", "%3", ended=True)
    server, ps = ListedPanes({}), Ps()

    report = _sample(server, ps)

    assert report.agents == [] and (ps.reads, server.asked) == (0, 0)


def test_the_tmux_listing_parses_pids_dead_panes_and_skips_odd_lines() -> None:
    class Raw(ListedPanes):
        def _answer(self, argv: Sequence[str], stdin: bytes | None) -> Completed:
            return Completed(0, "%1|~|500|~|0\n%2|~||~|1\nnonsense\n|~|7|~|0\n%3|~|x|~|0\n", "")

    assert Raw({}).pane_pids() == {"%1": 500, "%2": None, "%3": None}


# --- what each session spent ------------------------------------------------------------------


def _transcript(tmp_path: Path, name: str, *entries: dict[str, Any]) -> Path:
    return write(tmp_path / "transcripts" / f"{name}.jsonl", *entries)


def _session(project: ProjectInfo, session_id: str, transcript: Path) -> None:
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id=session_id,
                project_id=project.id,
                role="coder",
                started_at=NOW,
                last_seen_at=NOW,
                transcript_path=str(transcript),
            )
        )


def test_spend_starts_from_the_recorded_row_and_adds_only_what_the_transcript_gained(
    project: ProjectInfo, tmp_path: Path
) -> None:
    """The Stop hook recorded a total and where its scan stopped; the sample adds the request
    written since, and never reads the recorded bytes again (the recorded figure is not what
    those bytes hold: re-read, the total would be wrong)."""
    path = _transcript(tmp_path, "ses-a", request(NOW, "msg_1", cwd=project.root))
    _, state = usage_core.scan(path)
    recorded = usage_core.Usage(
        models={"claude-opus-5-5": usage_core.ModelUsage(requests=1, input=1_000_000, cost_usd=5)}
    )
    with store_session() as store:
        store.record_usage(
            SessionUsage(
                session_id="ses-a",
                project_id=project.id,
                transcript_path=str(path),
                state=state.to_json(),
                usage=recorded.to_dict(),
                started_at=NOW,
                updated_at=NOW,
            ),
            None,
            previous_state=None,
        )
    write(path, request(NOW + timedelta(minutes=1), "msg_2", cwd=project.root))
    _agent(project, "coder-1", "%1", session="ses-a")

    coder = _by_label(_sample(ListedPanes({"%1": 500}), Ps()))["coder-1"]

    assert coder.tokens == 1_000_000 + 10 + 100 + 1000 + 20
    assert coder.cost_usd is not None and coder.cost_usd > 5 and coder.priced


def test_a_session_no_hook_recorded_is_read_from_its_first_line_then_only_its_new_bytes(
    project: ProjectInfo, tmp_path: Path
) -> None:
    path = _transcript(
        tmp_path,
        "ses-b",
        request(NOW, "msg_1", cwd=project.root),
        request(NOW, "msg_2", cwd=project.root, output=500),
    )
    _session(project, "ses-b", path)
    _agent(project, "coder-1", "%1", session="ses-b")
    _agent(project, "fresh", "%2")  # no session yet: no spend
    server, spend = ListedPanes({"%1": 500, "%2": 600}), fleet_top.SpendReader()

    first = _by_label(_sample(server, Ps(), spend=spend))
    write(path, request(NOW + timedelta(minutes=1), "msg_3", cwd=project.root))
    second = _by_label(_sample(server, Ps(), spend=spend))

    assert first["coder-1"].tokens == 1130 + 1610
    assert second["coder-1"].tokens == 1130 + 1610 + 1130
    assert first["fresh"].tokens is None and first["fresh"].cost_usd is None
    _, _, state = spend._seen["ses-b"]
    assert state.offsets[str(path)] == path.stat().st_size, "the reader kept its place"


def test_a_spend_reader_forgets_a_session_whose_agent_ended(
    project: ProjectInfo, tmp_path: Path
) -> None:
    path = _transcript(tmp_path, "ses-c", request(NOW, "msg_1", cwd=project.root))
    _session(project, "ses-c", path)
    row = _agent(project, "coder-1", "%1", session="ses-c")
    spend = fleet_top.SpendReader()
    _sample(ListedPanes({"%1": 500}), Ps(), spend=spend)
    assert "ses-c" in spend._seen
    with store_session() as store:
        store.upsert_fleet_agent(row.model_copy(update={"ended_at": NOW}))

    _sample(ListedPanes({"%1": 500}), Ps(), spend=spend)

    assert spend._seen == {}


# --- sorting --------------------------------------------------------------------------------


def _use(label: str, *, cpu: float | None, rss: int | None, tokens: int | None) -> Any:
    return fleet_top.AgentUse(
        agent_id=label,
        project_id="p",
        project="shop",
        codename=None,
        label=label,
        role="coder",
        account=None,
        account_slot=None,
        session_id=None,
        pane_id="%1",
        state="running" if cpu is not None else "gone",
        cpu=cpu,
        rss_kb=rss,
        tokens=tokens,
    )


def test_each_sort_puts_the_heaviest_first_and_an_agent_with_no_figure_last() -> None:
    agents = [
        _use("a", cpu=1.0, rss=900, tokens=5),
        _use("b", cpu=50.0, rss=100, tokens=None),
        _use("c", cpu=None, rss=None, tokens=70),
        _use("d", cpu=50.0, rss=500, tokens=10),
    ]

    def order(by: fleet_top.SortKey) -> list[str]:
        return [a.label for a in fleet_top.sort_agents(agents, by)]

    assert order("cpu") == ["b", "d", "a", "c"]  # a tie by label
    assert order("mem") == ["a", "d", "b", "c"]
    assert order("tokens") == ["c", "d", "a", "b"]


# --- the command ------------------------------------------------------------------------------


@pytest.fixture
def faked(monkeypatch: pytest.MonkeyPatch) -> ListedPanes:
    """``fleet top``'s own sample, over the fake ps and a fake server."""
    server = ListedPanes({"%1": 500, "%2": 600})
    real = fleet_top.sample

    def sample(project_ids: Any = None, **kwargs: Any) -> fleet_top.TopReport:
        return real(project_ids, read_ps=Ps(), server_of=lambda row: server)

    monkeypatch.setattr(fleet_top, "sample", sample)
    return server


def test_fleet_top_prints_a_table_sorted_by_cpu_with_a_totals_row(
    project: ProjectInfo, faked: ListedPanes
) -> None:
    _agent(project, "manager", "%2", role="manager")
    _agent(project, "coder-1", "%1")
    _agent(project, "coder-gone", "%9")

    result = runner.invoke(app, ["fleet", "top"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].startswith("3 live agents · sampled in ") and "sorted by CPU" in lines[0]
    assert "CPU% ▼" in lines[1] and "TOKENS" in lines[1] and "COST" in lines[1]
    body = [line.split() for line in lines[2:5]]
    assert [row[1] for row in body] == ["coder-1", "manager", "coder-gone"]
    assert body[0][:5] == ["shop", "coder-1", "coder", "—", "99.0"]
    assert body[0][5:8] == ["875M", "4", "1h02m"]
    assert body[2][4] == "gone"
    total = next(line for line in lines if line.startswith("total"))
    assert total.split()[1:6] == ["3", "agents", "(2", "read)", "101.0"]


def test_fleet_top_json_is_one_object_with_every_agent_and_the_totals(
    project: ProjectInfo, faked: ListedPanes
) -> None:
    _agent(project, "coder-1", "%1")
    _agent(project, "manager", "%2", role="manager")

    result = runner.invoke(app, ["--json", "fleet", "top", "--sort", "mem"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["sort"] == "mem" and payload["ps_read"] is True
    assert [a["label"] for a in payload["agents"]] == ["coder-1", "manager"]
    first = payload["agents"][0]
    assert {k: first[k] for k in ("state", "pid", "cpu_percent", "rss_kb", "processes")} == {
        "state": "running",
        "pid": 500,
        "cpu_percent": 99.0,
        "rss_kb": 896_000,
        "processes": 4,
    }
    assert first["uptime_seconds"] == 3723 and first["tokens"] is None
    assert payload["totals"]["agents"] == 2 and payload["totals"]["processes"] == 5


def test_fleet_top_project_narrows_to_one_fleet_and_an_unknown_one_fails(
    project: ProjectInfo, faked: ListedPanes, tmp_path: Path
) -> None:
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = add_project(other_root)
    _agent(project, "coder-1", "%1")
    _agent(other, "elsewhere", "%2")

    narrowed = runner.invoke(app, ["--json", "fleet", "top", "-P", project.id])
    unknown = runner.invoke(app, ["--json", "fleet", "top", "-P", "no-such-project"])

    assert [a["label"] for a in json.loads(narrowed.output)["agents"]] == ["coder-1"]
    assert unknown.exit_code != 0 and json.loads(unknown.output)["error"] == "not_found"


def test_fleet_top_with_no_live_agent_says_so(project: ProjectInfo, faked: ListedPanes) -> None:
    result = runner.invoke(app, ["fleet", "top"])

    assert result.exit_code == 0
    assert result.output.splitlines()[-1].strip() == "(no live agents)"
    assert faked.asked == 0


def test_fleet_top_refuses_an_unknown_sort(project: ProjectInfo, faked: ListedPanes) -> None:
    result = runner.invoke(app, ["fleet", "top", "--sort", "disk"])

    assert result.exit_code == 2 and "cpu, mem, tokens" in result.output
