"""``project forget`` and ``project prune`` (#83).

The symptom: 305 registered projects on the owner's box, most of them throwaway
git worktrees, every one of which the fleet UI loaded state for before its first
frame. Nothing could remove a registration. These two commands can, and the
properties held here are the ones that make removal safe to offer:

- a plain forget is a TOMBSTONE — history stays, hidden, and comes back if the
  root is registered again; only ``--purge`` deletes;
- a project with live fleet agents is refused (exit 2), never forgotten;
- forgetting the active project moves the pin somewhere sensible and says so;
- prune shows its plan and drops nothing without ``--yes`` or a yes at a tty;
- a worktree is pruned only when its principal is itself registered.
"""

from __future__ import annotations

import itertools
import json
import shutil
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import paths
from aisquare.core.ids import new_agent_id, new_event_id, new_task_id
from aisquare.core.state_file import StateUnwritableError, read_state
from aisquare.core.store import SqliteStore, store_session
from aisquare.core.workspace import pinned_project_id, project_id_for, worktree_principal
from aisquare.models import FleetAgent, ProjectInfo, TeamEvent, TeamSession, TeamTask
from tests.fsperms import can_deny_reads
from tests.test_worktree import _git


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A neutral working directory; tests register projects under it."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    return workdir


@pytest.fixture
def repo_and_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A git repo plus a linked worktree on a feature branch (as in test_worktree)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
    worktree = tmp_path / "wt-feature"
    _git("worktree", "add", "-q", str(worktree), "-b", "feature", cwd=repo)
    return repo, worktree


def _json(output: str) -> Any:
    return json.loads(output)


def _register(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, directory: Path, note: str = "a note"
) -> str:
    """Register ``directory`` the way real use does — a fact written from inside it."""
    directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(directory)
    result = runner.invoke(app, ["context", "add", note, "--project"])
    assert result.exit_code == 0, result.output
    return project_id_for(directory.resolve())


def _listed(runner: CliRunner) -> set[str]:
    listed = runner.invoke(app, ["--json", "project", "list"])
    assert listed.exit_code == 0, listed.output
    return {project["id"] for project in _json(listed.stdout)}


def _raw(sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    connection = sqlite3.connect(str(paths.db_path()))
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def _raw_write(sql: str, params: tuple[Any, ...] = ()) -> None:
    connection = sqlite3.connect(str(paths.db_path()))
    try:
        connection.execute(sql, params)
        connection.commit()
    finally:
        connection.close()


def _record_turn(trace_id: str, project_id: str) -> None:
    """A metric row the way the hooks leave one even with the CI test bed off."""
    _raw_write(
        "INSERT INTO metric (trace_id, project_id, started_at, client_reason) "
        "VALUES (?, ?, '2026-09-01T00:00:00+00:00', 'disabled')",
        (trace_id, project_id),
    )


def _agent(project_id: str, label: str = "coder1") -> FleetAgent:
    return FleetAgent(
        id=new_agent_id(),
        project_id=project_id,
        label=label,
        role="coder",
        pane_id="%1",
        cwd=Path("/tmp"),
        created_at=datetime.now(tz=UTC),
    )


# --- forget --------------------------------------------------------------------


def test_forget_hides_the_registration_and_keeps_its_history(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha = _register(runner, monkeypatch, work_dir / "alpha")

    result = runner.invoke(app, ["project", "forget", "alpha"])

    assert result.exit_code == 0, result.output
    assert "forgot alpha" in result.stdout
    assert "stay in the store" in result.stdout
    assert _listed(runner) == set()
    # A tombstone, not a delete: the fact and the row are both still there.
    assert _raw("SELECT COUNT(*) FROM entry WHERE project_id = ?", (alpha,)) == [(1,)]
    (forgotten_at,) = _raw("SELECT forgotten_at FROM project WHERE id = ?", (alpha,))[0]
    assert forgotten_at is not None


def test_a_forgotten_project_comes_back_with_its_history_when_the_root_registers_again(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha = _register(runner, monkeypatch, work_dir / "alpha", "first note")
    assert runner.invoke(app, ["project", "forget", "alpha"]).exit_code == 0

    runner.invoke(app, ["context", "add", "second note", "--project"])  # still inside alpha

    assert _listed(runner) == {alpha}
    listed = runner.invoke(app, ["--json", "context", "list"])
    assert {entry["text"] for entry in _json(listed.stdout)} == {"first note", "second note"}


def test_forget_resolves_a_path_a_dot_and_an_id_prefix(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha = _register(runner, monkeypatch, work_dir / "alpha")
    beta = _register(runner, monkeypatch, work_dir / "beta")
    gamma = _register(runner, monkeypatch, work_dir / "gamma")

    by_path = runner.invoke(app, ["--json", "project", "forget", str(work_dir / "alpha")])
    assert by_path.exit_code == 0, by_path.output
    assert _json(by_path.stdout)["project"]["id"] == alpha

    by_prefix = runner.invoke(app, ["--json", "project", "forget", beta[:12]])
    assert by_prefix.exit_code == 0, by_prefix.output
    assert _json(by_prefix.stdout)["project"]["id"] == beta

    monkeypatch.chdir(work_dir / "gamma")
    by_dot = runner.invoke(app, ["--json", "project", "forget", "."])
    assert by_dot.exit_code == 0, by_dot.output
    assert _json(by_dot.stdout)["project"]["id"] == gamma
    assert _listed(runner) == set()


def test_forget_unknown_and_ambiguous_fail_the_way_switch_does(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unknown = runner.invoke(app, ["project", "forget", "ghost"])
    assert unknown.exit_code == 1
    assert "no project matches 'ghost'" in unknown.output
    as_json = runner.invoke(app, ["--json", "project", "forget", "ghost"])
    assert _json(as_json.stdout) == {"error": "not_found", "ref": "ghost"}

    _register(runner, monkeypatch, tmp_path / "x" / "app")
    _register(runner, monkeypatch, tmp_path / "y" / "app")
    ambiguous = runner.invoke(app, ["project", "forget", "app"])
    assert ambiguous.exit_code == 1
    assert "matches multiple projects" in ambiguous.output
    assert len(_listed(runner)) == 2, "an ambiguous forget must remove nothing"


def test_forget_refuses_a_project_with_live_fleet_agents(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha = _register(runner, monkeypatch, work_dir / "alpha")
    with store_session() as store:
        agent = store.upsert_fleet_agent(_agent(alpha, "coder1"))

    refused = runner.invoke(app, ["project", "forget", "alpha"])
    assert refused.exit_code == 2, refused.output
    assert "1 live fleet agent" in refused.output and "coder1" in refused.output
    assert "fleet stop" in refused.output
    as_json = runner.invoke(app, ["--json", "project", "forget", "alpha"])
    assert as_json.exit_code == 2
    assert _json(as_json.stdout)["error"] == "project_busy"
    assert _listed(runner) == {alpha}

    with store_session() as store:
        store.end_fleet_agent(agent.id, exit_status=0)
    assert runner.invoke(app, ["project", "forget", "alpha"]).exit_code == 0
    assert _listed(runner) == set()


def test_forget_refuses_a_hub_whose_board_another_projects_live_agents_read(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 32, 3/9: ``project forget`` refused only a project with live
    rows of its own. Beta's agents, spawned under hub alpha, read alpha's board, and alpha
    was forgotten to tidy the list. A switch on a usage limit gave beta's replacement alpha
    as its hub, and its ``launch`` added the board it joins (``team on``): alpha was back
    on the list with nothing to say why. A board another project's live agents read is
    refused, naming that project's stop; an ended row a newer one of its label replaced
    does not hold it (a stopped row a restart acts on does: round 33, 3/10, below)."""
    alpha = _register(runner, monkeypatch, work_dir / "alpha")
    beta = _register(runner, monkeypatch, work_dir / "beta")
    with store_session() as store:
        earlier = datetime.now(tz=UTC) - timedelta(hours=1)
        ended = store.upsert_fleet_agent(
            _agent(beta, "coder0").model_copy(update={"board": alpha, "created_at": earlier})
        )
        store.end_fleet_agent(ended.id, exit_status=0)
        store.upsert_fleet_agent(_agent(beta, "coder0"))  # its label's newest, on beta's board
        for label in ("manager", "coder1"):
            store.upsert_fleet_agent(_agent(beta, label).model_copy(update={"board": alpha}))
        store.upsert_fleet_agent(_agent(beta, "coder2"))  # on beta's own board

    for purge in ([], ["--purge"]):
        refused = runner.invoke(app, ["project", "forget", "alpha", *purge])
        assert refused.exit_code == 2, refused.output
        said = " ".join(refused.output.split())
        assert "alpha is the board beta's 2 live fleet agent(s) read: manager, coder1 —" in said
        assert f"fleet stop --project {beta} <label>" in said and "coder2" not in said
        assert "coder0" not in said and "(stopped)" not in said
        as_json = runner.invoke(app, ["--json", "project", "forget", "alpha", *purge])
        assert _json(as_json.stdout) == {"error": "project_busy", "ref": alpha}
    assert _listed(runner) == {alpha, beta}


def test_forget_refuses_a_hub_a_restart_of_another_projects_stopped_agent_reads_again(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 33, 3/10: round 32's refusal asked the live rows alone, but a
    restart acts on a stopped row too (the 💤 row), and starts its replacement under the hub
    its launch recorded, whose ``launch`` adds the board it joins. Beta's manager, spawned
    under hub alpha, stopped; alpha was forgotten with nothing to refuse it, and Restart on
    the manager brought it back. A stopped row a restart acts on (the newest of its label)
    holds the board as a live one does, and the refusal says what frees it; once beta is
    forgotten, nothing lists its rows to restart, and alpha goes."""
    alpha = _register(runner, monkeypatch, work_dir / "alpha")
    beta = _register(runner, monkeypatch, work_dir / "beta")
    with store_session() as store:
        for label in ("manager", "coder1"):
            row = store.upsert_fleet_agent(_agent(beta, label).model_copy(update={"board": alpha}))
            store.end_fleet_agent(row.id, exit_status=0)
        live = store.upsert_fleet_agent(_agent(beta, "coder2").model_copy(update={"board": alpha}))

    refused = runner.invoke(app, ["project", "forget", "alpha"])
    assert refused.exit_code == 2, refused.output
    said = " ".join(refused.output.split())
    assert "alpha is the board beta's 3 fleet agent(s) read: manager (stopped), coder1 " in said
    assert "(stopped), coder2 — stop the live ones first" in said, said
    with store_session() as store:
        store.end_fleet_agent(live.id, exit_status=0)
    for purge in ([], ["--purge"]):
        refused = runner.invoke(app, ["project", "forget", "alpha", *purge])
        assert refused.exit_code == 2, refused.output
        said = " ".join(refused.output.split())
        assert "beta's 3 fleet agent(s) read: manager (stopped)," in said and "stop the" not in said
        assert "forget beta first if you are done with its agents" in said, said
    assert _listed(runner) == {alpha, beta}

    assert runner.invoke(app, ["project", "forget", "beta"]).exit_code == 0
    assert runner.invoke(app, ["project", "forget", "alpha"]).exit_code == 0
    assert _listed(runner) == set()


class _Unwalked(list[FleetAgent]):
    """Another board's rows, which a lookup of one board must not walk."""

    def __iter__(self) -> Any:
        raise AssertionError("a lookup of one board walked another board's rows")


def test_what_reads_a_board_is_looked_up_by_the_board_not_over_every_row(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 37, 6/7: ``_board_readers`` walked every row that holds a board,
    in every project, for each project it was asked of: each candidate of a prune on each
    pass of its holds, and each captured row doctor asks of at every ``asq`` start, 1.5 M
    row checks a pass for 300 rows over 5 000 heads. The rows are grouped by the board each
    recorded once (``reading_rows``), and a lookup reads only its board's."""
    from aisquare.services import project as project_service

    tools = _register(runner, monkeypatch, work_dir / "tools")
    api = _register(runner, monkeypatch, work_dir / "api")
    web = _register(runner, monkeypatch, work_dir / "web")
    others = [_register(runner, monkeypatch, work_dir / f"other{n}") for n in range(3)]
    with store_session() as store:
        manager = store.upsert_fleet_agent(
            _agent(api, "manager").model_copy(update={"board": tools})
        )
        store.end_fleet_agent(manager.id, exit_status=0)
        store.upsert_fleet_agent(_agent(web, "coder1").model_copy(update={"board": tools}))
        for other in others:
            store.upsert_fleet_agent(_agent(other, "coder1"))
        known = {each.id: each for each in store.list_projects(all=True)}
        rows = project_service.reading_rows(store)
        assert set(rows) == {tools, *others}, "by the board each row recorded"
        assert [(row.project_id, row.label) for row in rows[tools]] == [
            (api, "manager"),
            (web, "coder1"),
        ]
        readers = {board: rows[board] if board == tools else _Unwalked() for board in rows}

        refused = project_service.forget_refusals(
            store, [known[tools], known[api]], readers=readers
        )

    assert list(refused) == [tools], "nothing reads api's board"
    assert [reader.project.id for reader in refused[tools].readers] == [api, web]


def test_forget_of_a_hub_two_projects_read_names_both_with_what_frees_each(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 35, 2/8: the refusal named the first project whose rows read the
    hub, alone. Tools is read by api's stopped manager and by web's running coder: the
    refusal named api and its forget, and once api was forgotten, forget of tools was
    refused again, naming web. Every reader is named, each with what frees it: web's stop,
    api's forget. A reader that is forgotten, its coder still running, is told to be
    onboarded first: no fleet command finds a forgotten project."""
    from aisquare.services import install_route
    from aisquare.services import project as project_service

    tools = _register(runner, monkeypatch, work_dir / "tools")
    api = _register(runner, monkeypatch, work_dir / "api")
    web = _register(runner, monkeypatch, work_dir / "web")
    with store_session() as store:
        manager = store.upsert_fleet_agent(
            _agent(api, "manager").model_copy(update={"board": tools})
        )
        store.end_fleet_agent(manager.id, exit_status=0)
        coder = store.upsert_fleet_agent(_agent(web, "coder1").model_copy(update={"board": tools}))
        known = {each.id: each for each in store.list_projects(all=True)}

    def forget(name: str) -> str:
        return install_route.command_line(["aisquare", "project", "forget", str(work_dir / name)])

    refused = runner.invoke(app, ["project", "forget", "tools"])
    assert refused.exit_code == 2, refused.output
    said = " ".join(refused.output.split())
    assert "tools is the board api's 1 fleet agent(s) read: manager (stopped) — " in said, said
    assert f"forget api first if you are done with its agents ({forget('api')})" in said, said
    assert (
        "It is also the board web's 1 live fleet agent(s) read: coder1 — stop them first " in said
    )
    assert f"({project_service.fleet_command('stop', known[web])} <label>)" in said, said
    with store_session() as store:
        [busy] = project_service.forget_refusals(store, [known[tools]]).values()
    assert [reader.project.id for reader in busy.readers] == [api, web]
    assert [(r.has_live, r.forgotten) for r in busy.readers] == [(False, False), (True, False)]

    with store_session() as store:
        store.forget_project(web)  # its coder still running: a forgotten project's rows hold
    refused = runner.invoke(app, ["project", "forget", "tools"])
    said = " ".join(refused.output.split())
    onboard = project_service.onboard_command(work_dir / "web")
    assert f"coder1 — web is forgotten: {onboard} adds it back first; stop them first" in said, said

    assert runner.invoke(app, ["project", "forget", "api"]).exit_code == 0
    with store_session() as store:
        store.end_fleet_agent(coder.id, exit_status=0)  # a forgotten project's stopped row: none
    assert runner.invoke(app, ["project", "forget", "tools"]).exit_code == 0


@pytest.mark.parametrize("forgotten", [False, True], ids=["registered", "forgotten"])
def test_forget_of_a_hub_a_running_reader_holds_gives_its_stop_and_then_its_forget(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch, forgotten: bool
) -> None:
    """Review of #259, round 36, 1/9: a reader whose rows were all live was told to stop them,
    and no more. Stopped, a row is still the newest of its label, a restart away from the
    hub, so ``forget tools`` was refused again after the stop, now naming web's forget. Its
    forget comes after its stop, and after its onboard when it is forgotten: each step
    followed in turn, the hub goes."""
    from aisquare.services import install_route
    from aisquare.services import project as project_service

    tools = _register(runner, monkeypatch, work_dir / "tools")
    web = _register(runner, monkeypatch, work_dir / "web")
    with store_session() as store:
        coder = store.upsert_fleet_agent(_agent(web, "coder1").model_copy(update={"board": tools}))
        if forgotten:
            store.forget_project(web)
    forget = install_route.command_line(["aisquare", "project", "forget", str(work_dir / "web")])

    refused = runner.invoke(app, ["project", "forget", "tools"])
    assert refused.exit_code == 2, refused.output
    said = " ".join(refused.output.split())
    stop = said.index("stop them first (")
    then = said.index(f"then forget web if you are done with its agents ({forget})")
    assert stop < then, said
    if forgotten:
        assert said.index(project_service.onboard_command(work_dir / "web")) < stop, said
        assert runner.invoke(app, ["project", "onboard", str(work_dir / "web")]).exit_code == 0

    with store_session() as store:
        store.end_fleet_agent(coder.id, exit_status=0)  # its stop
    refused = runner.invoke(app, ["project", "forget", "tools"])
    assert refused.exit_code == 2, "a stopped row holds the hub, as the forget step said"
    assert f"forget web first if you are done with its agents ({forget})" in " ".join(
        refused.output.split()
    )
    assert runner.invoke(app, ["project", "forget", str(work_dir / "web")]).exit_code == 0
    assert runner.invoke(app, ["project", "forget", "tools"]).exit_code == 0


def test_forgets_help_and_docs_say_a_stopped_reader_holds_a_hub_too() -> None:
    """Review of #259, round 34, 8/10: since round 33 (3/10) the refusal covers the stopped
    rows a restart would start under the hub, but ``project forget``'s help, its service's
    docstring and docs/fleet.md said "live" alone: someone with no live agents anywhere did
    not expect the refusal they got. Each says live or stopped, as the CHANGELOG does."""
    from aisquare.services import project as project_service

    shown = CliRunner().invoke(app, ["project", "forget", "--help"], env={"COLUMNS": "200"})
    assert shown.exit_code == 0, shown.output
    said = {
        "help": " ".join(shown.stdout.replace("│", " ").split()),
        "docstring": " ".join((project_service.forget_project.__doc__ or "").split()),
        "docs": " ".join(
            (Path(__file__).parents[1] / "docs" / "fleet.md").read_text(encoding="utf-8").split()
        ),
    }
    assert (
        "another project's fleet agents read its board (a hub's), live or stopped" in said["help"]
    ), said["help"]
    assert "another project's fleet agents read its board, as a hub's" in said["docstring"]
    assert "live or stopped" in said["docstring"]
    assert "a hub whose board another project's agents read, live or stopped" in said["docs"]
    for where, text in said.items():
        assert "project's live fleet agents read" not in text, where
        assert "project's live agents read" not in text, where


def test_forget_the_active_project_moves_the_pin_to_the_most_recently_touched(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha = _register(runner, monkeypatch, work_dir / "alpha")
    _register(runner, monkeypatch, work_dir / "beta")
    gamma = _register(runner, monkeypatch, work_dir / "gamma")
    # alpha was registered first but is touched LAST, so it is the one to land on.
    monkeypatch.chdir(work_dir / "alpha")
    runner.invoke(app, ["context", "add", "a later note", "--project"])
    assert runner.invoke(app, ["project", "switch", "gamma"]).exit_code == 0
    assert pinned_project_id() == gamma

    result = runner.invoke(app, ["project", "forget", "gamma"])

    assert result.exit_code == 0, result.output
    assert "active project is now alpha" in result.stdout
    assert pinned_project_id() == alpha
    info = runner.invoke(app, ["--json", "project", "info"])
    assert _json(info.stdout)["id"] == alpha


def test_forget_the_cwd_project_is_forgetting_the_active_one(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Active means pinned OR cwd-derived; with no pin, the cwd project is active."""
    alpha = _register(runner, monkeypatch, work_dir / "alpha")
    _register(runner, monkeypatch, work_dir / "beta")  # cwd is now beta, nothing pinned
    assert pinned_project_id() is None

    result = runner.invoke(app, ["--json", "project", "forget", "beta"])

    report = _json(result.stdout)
    assert report["active_changed"] is True
    assert report["active"]["id"] == alpha
    assert pinned_project_id() == alpha


def test_forget_the_only_project_clears_the_pin(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(runner, monkeypatch, work_dir / "alpha")
    assert runner.invoke(app, ["project", "switch", "alpha"]).exit_code == 0

    result = runner.invoke(app, ["project", "forget", "alpha"])

    assert result.exit_code == 0, result.output
    assert "follows your working directory" in result.stdout
    assert pinned_project_id() is None
    assert "No projects registered yet" in runner.invoke(app, ["project", "list"]).stdout


def test_forget_purge_deletes_every_row_the_project_owns_and_its_data_dir(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha = _register(runner, monkeypatch, work_dir / "alpha")
    beta = _register(runner, monkeypatch, work_dir / "beta")  # the bystander
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.add_prompt("hello", alpha)
        session = store.upsert_session(
            TeamSession(id="sess-alpha", project_id=alpha, started_at=now, last_seen_at=now)
        )
        store.upsert_task(
            TeamTask(
                id=new_task_id(),
                project_id=alpha,
                key="t1",
                title="a task",
                created_at=now,
                updated_at=now,
            )
        )
        store.add_team_event(
            TeamEvent(id=new_event_id(), project_id=alpha, text="an event", created_at=now)
        )
        ended = store.upsert_fleet_agent(_agent(alpha))
        store.end_fleet_agent(ended.id, exit_status=0)
        store.set_meta(f"distill_seq:{alpha}", "3")
        store.set_meta(f"signal/{alpha}/phase", "{}")
        store.set_meta(f"nudge:{session.id}", now.isoformat())
        store.set_meta(f"distill_seq:{beta}", "7")
    _record_turn("trc_alpha", alpha)
    _record_turn("trc_beta", beta)
    data_dir = paths.project_data_dir(alpha)
    (data_dir / "snapshot").mkdir(parents=True)
    (data_dir / "snapshot" / "pack.xml").write_text("<pack/>", encoding="utf-8")

    result = runner.invoke(app, ["--json", "project", "forget", "alpha", "--purge"])

    assert result.exit_code == 0, result.output
    report = _json(result.stdout)
    assert report["purged"] is True and report["data_dir_removed"] is True
    assert report["removed"] == {
        "entry": 1,
        "prompt": 1,
        "team_event": 1,
        "team_task": 1,
        "team_session": 1,
        "fleet_agent": 1,
        "metric": 1,
        "project_setting": 0,
        "project_explainability": 0,
        "project_destination": 0,
        "team_meta": 3,
        "project": 1,
    }
    for table in (
        "entry",
        "prompt",
        "team_session",
        "team_task",
        "team_event",
        "fleet_agent",
        "metric",
    ):
        assert _raw(f"SELECT COUNT(*) FROM {table} WHERE project_id = ?", (alpha,)) == [(0,)]
    assert _raw("SELECT COUNT(*) FROM project WHERE id = ?", (alpha,)) == [(0,)]
    assert not data_dir.exists()
    # The bystander kept everything.
    assert _listed(runner) == {beta}
    assert _raw("SELECT COUNT(*) FROM entry WHERE project_id = ?", (beta,)) == [(1,)]
    assert _raw("SELECT value FROM team_meta WHERE key = ?", (f"distill_seq:{beta}",)) == [("7",)]
    assert _raw("SELECT COUNT(*) FROM metric WHERE project_id = ?", (beta,)) == [(1,)]


def test_purge_copes_with_hundreds_of_recorded_sessions(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """500 sessions once meant 1,002 ``OR`` terms in one DELETE, and SQLite's
    ``Expression tree is too large`` rolled the purge back with the project
    intact — even with nothing in ``team_meta`` to delete."""
    alpha = _register(runner, monkeypatch, work_dir / "alpha")
    now = datetime.now(tz=UTC)
    with store_session() as store:
        for i in range(501):
            session = store.upsert_session(
                TeamSession(id=f"sess-{i:03d}", project_id=alpha, started_at=now, last_seen_at=now)
            )
            store.set_meta(f"nudge:{session.id}", now.isoformat())
        store.set_meta("continuations:sess-000:2026-09-09T12", "1")
        store.set_meta("nudge:sess-elsewhere", now.isoformat())  # not alpha's

    result = runner.invoke(app, ["--json", "project", "forget", "alpha", "--purge"])

    assert result.exit_code == 0, result.output
    report = _json(result.stdout)
    assert report["removed"]["team_session"] == 501
    assert report["removed"]["team_meta"] == 502
    assert _raw("SELECT key FROM team_meta") == [("nudge:sess-elsewhere",)]
    assert _raw("SELECT COUNT(*) FROM project WHERE id = ?", (alpha,)) == [(0,)]


#: Values a CHECK needs that no made-up value meets, per table and column. A table a
#: migration adds whose CHECK refuses the made-up row fails the test below with the
#: constraint named; its values go here.
_CHECKED_VALUES: dict[str, dict[str, object]] = {"entry": {"pool": "project"}}

#: Distinct made-up values, so a UNIQUE column takes every one.
_MADE_UP = itertools.count(1)


def _pointing_at_a_project(connection: sqlite3.Connection) -> dict[str, list[str]]:
    """Every table and the columns in it that hold a project's id, read off the schema.

    A column whose foreign key references ``project (id)`` does, whatever it is
    called; so does a column named ``project_id`` with no FK, which is how the team
    tables, ``fleet_agent`` and ``metric`` are keyed. ``project`` itself is not one.
    """
    pointing: dict[str, list[str]] = {}
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "AND name != 'project' ORDER BY rowid"
    ).fetchall()
    for (table,) in tables:
        columns = [
            str(fk[3])
            for fk in connection.execute("SELECT * FROM pragma_foreign_key_list(?)", (table,))
            if str(fk[2]).lower() == "project" and fk[4] in (None, "id")
        ]
        names = [str(col[1]) for col in connection.execute(f'PRAGMA table_info("{table}")')]
        if "project_id" in names and "project_id" not in columns:
            columns.append("project_id")
        if columns:
            pointing[table] = columns
    return pointing


def _a_row(connection: sqlite3.Connection, table: str, fixed: dict[str, object]) -> int:
    """Insert a row into ``table`` with ``fixed``, making up every other required value.

    Required is NOT NULL with no default, or a primary key that is not the rowid.
    A required column whose foreign key references another table gets a row of its
    own there first, so the insert holds under ``PRAGMA foreign_keys = ON`` however
    the schema grows. The rowid is returned, for a child to read its key back.
    """
    parents = {
        str(fk[3]): (str(fk[2]), fk[4])
        for fk in connection.execute("SELECT * FROM pragma_foreign_key_list(?)", (table,))
    }
    values = {**_CHECKED_VALUES.get(table, {}), **fixed}
    for _cid, name, declared, notnull, default, pk in connection.execute(
        f'PRAGMA table_info("{table}")'
    ).fetchall():
        is_rowid = pk == 1 and str(declared).upper() == "INTEGER"
        if name in values or is_rowid or not ((notnull and default is None) or pk):
            continue
        if name in parents:
            parent, key = parents[name]
            made = _a_row(connection, parent, {})
            (values[name],) = connection.execute(
                f'SELECT "{key or "rowid"}" FROM "{parent}" WHERE rowid = ?', (made,)
            ).fetchone()
            continue
        made_up = next(_MADE_UP)
        values[name] = made_up if "INT" in str(declared).upper() else f"{table}-{made_up}"
    names = ", ".join(f'"{name}"' for name in values)
    marks = ", ".join("?" * len(values))
    try:
        cursor = connection.execute(
            f'INSERT INTO "{table}" ({names}) VALUES ({marks})', tuple(values.values())
        )
    except sqlite3.IntegrityError as exc:
        pytest.fail(f"no made-up row fits {table} ({exc}); add what it needs to _CHECKED_VALUES")
    return int(cursor.lastrowid or 0)


def test_purge_empties_every_table_that_points_at_the_project_whatever_the_schema_adds(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row in ANY table that holds the project's id goes with the project. The tables
    are found by walking the schema, not read from a list, so one a later migration adds
    is checked the day it lands.

    A list is what kept failing: each branch that added a table with a foreign key to
    ``project`` listed its own in ``purge_project`` and not the others', and one
    missing table was enough for ``FOREIGN KEY constraint failed`` to roll the whole
    purge back — ``forget --purge`` could not remove exactly the projects the new
    features had been used on (review of #172, and of the #205 fold). A table keyed by
    ``project_id`` WITHOUT a foreign key refuses nothing, so a purge that skipped it
    succeeded and left its rows for the project's next registration; this fails on
    that too. The bystander keeps its row in every table.
    """
    alpha = _register(runner, monkeypatch, work_dir / "alpha")
    beta = _register(runner, monkeypatch, work_dir / "beta")
    connection = sqlite3.connect(str(paths.db_path()))
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        # What a later migration adds, the way #170's and #172's did: a foreign key to
        # the project, here through a column not called project_id and to its implied
        # primary key.
        connection.execute(
            "CREATE TABLE a_later_feature (owner TEXT NOT NULL REFERENCES project, "
            "note TEXT NOT NULL, PRIMARY KEY (owner, note))"
        )
        pointing = _pointing_at_a_project(connection)
        # The control: the walk found the tables this tree is known to have.
        known = {"entry", "prompt", "project_setting", "team_session", "metric", "a_later_feature"}
        assert known <= set(pointing), pointing
        for table, columns in pointing.items():
            for project in (alpha, beta):
                _a_row(connection, table, dict.fromkeys(columns, project))
        connection.commit()
    finally:
        connection.close()
    # The one table keyed by the project that a purge must NOT empty: a revocation
    # owed for a key the CLI minted (#142) outlives its project — it is how that key
    # is still revoked after the purge that took its destination row.
    outliving = {"pending_revocation": pointing.pop("pending_revocation")}

    with store_session() as store:  # the store's own purge: made-up rows are no models
        removed = store.purge_project(alpha)

    assert removed["project"] == 1
    for table, columns in pointing.items():
        for column in columns:
            count = f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" = ?'
            assert _raw(count, (alpha,)) == [(0,)], f"{table}.{column} kept the purged project"
            assert _raw(count, (beta,)) != [(0,)], f"{table}.{column} lost the bystander's row"
    for table, columns in outliving.items():
        for column in columns:
            count = f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" = ?'
            assert _raw(count, (alpha,)) != [(0,)], f"{table}.{column} went with the project"
    assert _raw("SELECT COUNT(*) FROM project WHERE id = ?", (alpha,)) == [(0,)]
    assert _raw("PRAGMA foreign_key_check") == []


def test_a_forgotten_projects_facts_are_hidden_from_context_reads_until_it_registers_again(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cwd still resolves to the project's id after a forget, so without the
    tombstone on the reads ``project list`` was empty while ``context list`` and
    ``inject`` went on serving the forgotten project's facts."""
    alpha = _register(runner, monkeypatch, work_dir / "alpha", "first note")
    with store_session() as store:
        store.add_prompt("hello", alpha)
    assert runner.invoke(app, ["project", "forget", "alpha"]).exit_code == 0

    listed = runner.invoke(app, ["--json", "context", "list"])  # still inside alpha

    assert listed.exit_code == 0, listed.output
    assert _json(listed.stdout) == []
    with store_session() as store:
        assert store.entries(project_id=alpha) == []
        assert store.entries(pool="project", project_id=alpha) == []
        assert store.search("first", project_id=alpha) == []
        assert store.recent_prompts(project_id=alpha) == []
        assert store.recent_prompts() == [], "the cross-project view hides it too"
    # The rows are still there, and registering the root brings every one back.
    assert _raw("SELECT COUNT(*) FROM entry WHERE project_id = ?", (alpha,)) == [(1,)]
    runner.invoke(app, ["context", "add", "second note", "--project"])
    with store_session() as store:
        assert {entry.text for entry in store.entries(project_id=alpha)} == {
            "first note",
            "second note",
        }
        assert [prompt.text for prompt in store.recent_prompts(project_id=alpha)] == ["hello"]


def test_a_project_with_history_cannot_be_deleted_by_hand_which_is_why_forget_tombstones(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The FK from ``entry`` (and ``prompt``) to ``project`` is real, and it is
    what decides the design: a plain forget cannot DELETE a project with any
    context, so it tombstones; ``purge_project`` deletes dependents first."""
    alpha = _register(runner, monkeypatch, work_dir / "alpha")

    connection = sqlite3.connect(str(paths.db_path()))
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM project WHERE id = ?", (alpha,))
    finally:
        connection.close()

    with store_session() as store:
        store.set_project_setting(alpha, "claude_account", "2")  # v15's FK-carrying table
        removed = store.purge_project(alpha)
    assert removed["entry"] == 1 and removed["project"] == 1
    # Left out of the purge, this row rolled the whole transaction back with a
    # FOREIGN KEY traceback (review of #205, second round).
    assert removed["project_setting"] == 1
    with store_session() as store:
        assert store.project_setting(alpha, "claude_account") is None


# --- prune ---------------------------------------------------------------------


def test_prune_missing_is_a_dry_run_until_yes(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha = _register(runner, monkeypatch, work_dir / "alpha")
    gone = _register(runner, monkeypatch, work_dir / "gone")
    monkeypatch.chdir(work_dir)
    shutil.rmtree(work_dir / "gone")

    as_json = runner.invoke(app, ["--json", "project", "prune", "--missing"])
    assert as_json.exit_code == 0, as_json.output
    plan = _json(as_json.stdout)
    assert plan["dry_run"] is True and plan["dropped"] == []
    assert [(c["project"]["id"], c["reason"]) for c in plan["candidates"]] == [(gone, "missing")]
    assert _listed(runner) == {alpha, gone}, "--json without --yes must change nothing"

    # Not a tty (the test runner's stdin never is): a dry run, said in words.
    human = runner.invoke(app, ["project", "prune", "--missing"])
    assert human.exit_code == 0, human.output
    assert "dry run" in human.stdout and "--yes" in human.stdout
    assert "gone" in human.stdout and "missing" in human.stdout
    assert _listed(runner) == {alpha, gone}

    dropped = runner.invoke(app, ["project", "prune", "--missing", "--yes"])
    assert dropped.exit_code == 0, dropped.output
    assert "forgot 1 registration" in dropped.stdout
    assert _listed(runner) == {alpha}


def test_prune_never_calls_a_root_it_may_not_search_missing(tmp_path: Path) -> None:
    """Review of #259, round 19: a root ``Path.is_dir`` could not ask was not missing, as it
    raised ``EACCES``; from Python 3.14 it says False instead, and ``prune --missing``
    dropped a project whose directory was there, under a folder this user may not search.
    Asked of a real such folder, on whichever interpreter runs this."""
    from aisquare.services import project as project_service

    if sys.platform == "win32" or not can_deny_reads():
        pytest.skip("needs a folder this user cannot search")
    locked = tmp_path / "locked"
    (locked / "there").mkdir(parents=True)
    with store_session() as store:
        for name, root in (("there", locked / "there"), ("gone", tmp_path / "gone")):
            store.onboard_project(ProjectInfo(id=f"prj_{name}", root=root))
    locked.chmod(0)
    try:
        plan = project_service.prune_candidates(missing=True, worktrees=False)
    finally:
        locked.chmod(0o700)

    assert [(c.project.id, c.reason) for c in plan] == [("prj_gone", "missing")]


def test_prune_with_nothing_to_drop_says_so(runner: CliRunner) -> None:
    result = runner.invoke(app, ["project", "prune"])
    assert result.exit_code == 0, result.output
    assert "nothing to prune" in result.stdout
    as_json = runner.invoke(app, ["--json", "project", "prune", "--yes"])
    assert _json(as_json.stdout)["candidates"] == []


def test_worktree_principal_reads_the_git_file_and_ignores_a_main_checkout(
    repo_and_worktree: tuple[Path, Path], tmp_path: Path
) -> None:
    repo, worktree = repo_and_worktree
    assert worktree_principal(worktree) == repo.resolve()
    assert worktree_principal(repo) is None, "a main checkout has a .git DIRECTORY"
    plain = tmp_path / "plain"
    plain.mkdir()
    assert worktree_principal(plain) is None

    # No subprocess and no principal on disk: the file alone is enough, which is
    # what makes a sweep over hundreds of registrations cheap and still right
    # for a worktree whose repository has since been deleted.
    orphan = tmp_path / "orphan"
    orphan.mkdir()
    (orphan / ".git").write_text("gitdir: /nowhere/repo/.git/worktrees/orphan\n", encoding="utf-8")
    # `.resolve()` on both sides, because the function returns
    # `principal.resolve()` and a rooted-but-driveless path is not absolute on
    # Windows: `Path("/nowhere/repo")` resolves against the current drive, so a
    # bare literal compares an anchored path against an unanchored one.
    assert worktree_principal(orphan) == Path("/nowhere/repo").resolve()
    bare = tmp_path / "of-bare"
    bare.mkdir()
    (bare / ".git").write_text("gitdir: /srv/repo.git/worktrees/x\n", encoding="utf-8")
    assert worktree_principal(bare) == Path("/srv/repo.git").resolve()  # anchored, as above


def _register_worktree_as_its_own_project(worktree: Path) -> str:
    """The registration shape the issue measured: a worktree with its OWN row.

    Registering through the CLI cannot produce it any more — a worktree resolves
    to its principal — so the row is written directly, as whatever made the 305
    of them did.
    """
    root = worktree.resolve()
    with store_session() as store:
        # On purpose: the rows the issue measured were LISTED worktrees (#139 hides captures).
        store.onboard_project(ProjectInfo(id=project_id_for(root), root=root, linked_repos=[]))
    return project_id_for(root)


def test_prune_worktrees_drops_a_worktree_only_when_its_principal_is_registered(
    runner: CliRunner, repo_and_worktree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, worktree = repo_and_worktree
    wt_id = _register_worktree_as_its_own_project(worktree)

    alone = runner.invoke(app, ["--json", "project", "prune", "--worktrees", "--yes"])
    assert alone.exit_code == 0, alone.output
    assert _json(alone.stdout)["candidates"] == [], (
        "a worktree of an UNREGISTERED repo is the only handle on that repo's context"
    )
    assert _listed(runner) == {wt_id}

    repo_id = _register(runner, monkeypatch, repo)
    result = runner.invoke(app, ["--json", "project", "prune", "--worktrees", "--yes"])
    assert result.exit_code == 0, result.output
    report = _json(result.stdout)
    assert report["dropped"] == [wt_id]
    (candidate,) = report["candidates"]
    assert candidate["reason"] == "worktree"
    assert candidate["principal"]["id"] == repo_id
    assert _listed(runner) == {repo_id}


def test_prune_with_no_selector_considers_both_reasons(
    runner: CliRunner,
    repo_and_worktree: tuple[Path, Path],
    work_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, worktree = repo_and_worktree
    wt_id = _register_worktree_as_its_own_project(worktree)
    repo_id = _register(runner, monkeypatch, repo)
    gone = _register(runner, monkeypatch, work_dir / "gone")
    monkeypatch.chdir(work_dir)
    shutil.rmtree(work_dir / "gone")

    result = runner.invoke(app, ["--json", "project", "prune", "--yes"])

    assert result.exit_code == 0, result.output
    assert set(_json(result.stdout)["dropped"]) == {wt_id, gone}
    assert _listed(runner) == {repo_id}


def test_prune_keeps_a_candidate_with_live_agents_and_says_so(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha = _register(runner, monkeypatch, work_dir / "alpha")
    busy = _register(runner, monkeypatch, work_dir / "busy")
    gone = _register(runner, monkeypatch, work_dir / "gone")
    monkeypatch.chdir(work_dir)
    shutil.rmtree(work_dir / "busy")
    shutil.rmtree(work_dir / "gone")
    with store_session() as store:
        store.upsert_fleet_agent(_agent(busy, "manager"))

    result = runner.invoke(app, ["project", "prune", "--missing", "--yes"])

    assert result.exit_code == 0, result.output
    assert "forgot 1 registration" in result.stdout
    assert "kept 1 with live fleet agents" in result.stdout
    assert _listed(runner) == {alpha, busy}
    plan = runner.invoke(app, ["--json", "project", "prune", "--missing"])
    assert [c["project"]["id"] for c in _json(plan.stdout)["kept"]] == [busy]
    del gone


@pytest.mark.parametrize("purge", [[], ["--purge"]], ids=["forget", "purge"])
def test_prune_keeps_a_hub_whose_board_another_projects_agents_read(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch, purge: list[str]
) -> None:
    """Review of #259, round 33, 2/10: round 32's refusal of a board another project's live
    agents read was in ``project forget`` alone. ``prune`` asked only for the candidate's own
    live rows: a hub whose folder held no code, deleted, was ``missing``, and ``prune
    --missing --purge --yes`` deleted the tasks, events and sessions of the board beta's
    manager and coders were claiming tasks on. Without ``--purge`` it was tombstoned, as
    ``forget`` refuses. Both ask one guard, so a hub is kept and named, plan and sweep."""
    hub = _register(runner, monkeypatch, work_dir / "hub")
    beta = _register(runner, monkeypatch, work_dir / "beta")
    gone = _register(runner, monkeypatch, work_dir / "gone")
    monkeypatch.chdir(work_dir)
    shutil.rmtree(work_dir / "hub")
    shutil.rmtree(work_dir / "gone")
    with store_session() as store:
        store.upsert_fleet_agent(_agent(beta, "manager").model_copy(update={"board": hub}))
        now = datetime.now(tz=UTC)
        store.upsert_task(
            TeamTask(
                id=new_task_id(), project_id=hub, key="t1", title="beta's work",
                created_at=now, updated_at=now,
            )
        )  # fmt: skip

    plan = runner.invoke(app, ["--json", "project", "prune", "--missing", *purge])
    (kept,) = _json(plan.stdout)["kept"]
    readers = [reader["id"] for reader in kept["read_by"]]
    assert (kept["project"]["id"], readers, kept["live_agents"]) == (hub, [beta], 0)
    result = runner.invoke(app, ["project", "prune", "--missing", "--yes", *purge])

    assert result.exit_code == 0, result.output
    said = " ".join(result.stdout.split())
    assert "missing (kept: the fleet agents of beta" in said, said  # cut to the width
    assert "kept 1 whose board another project's fleet agents read, as a hub's" in said
    assert _listed(runner) == {hub, beta}
    with store_session() as store:
        assert [t.title for t in store.team_tasks(hub)] == ["beta's work"]
        assert (store.get_project(gone, include_forgotten=True) is None) == bool(purge)
    forget = runner.invoke(app, ["--json", "project", "forget", hub, *purge])
    assert _json(forget.stdout) == {"error": "project_busy", "ref": hub}, "the same guard"


def test_a_prunes_reason_for_a_hub_reads_as_every_readers_and_the_changelog_says_no_more(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 36, 6/9: the CHANGELOG said ``project prune`` keeps a hub "naming
    every project that reads it, each with what frees it", but prune's table names the
    readers alone, which is ``forget``'s to add. And "(kept: the board api, web's fleet agents
    read)" read as if the board were web's alone. The reason names each reader as one of
    several, early in the cell, and the CHANGELOG gives what frees each to ``forget`` alone."""
    hub = _register(runner, monkeypatch, work_dir / "hub")
    readers = [_register(runner, monkeypatch, work_dir / name) for name in ("api", "web")]
    monkeypatch.chdir(work_dir)
    shutil.rmtree(work_dir / "hub")
    with store_session() as store:
        for reader in readers:
            store.upsert_fleet_agent(_agent(reader, "coder1").model_copy(update={"board": hub}))

    result = runner.invoke(app, ["project", "prune", "--missing", "--yes"], env={"COLUMNS": "400"})

    assert result.exit_code == 0, result.output
    said = " ".join(result.stdout.split())
    assert "missing (kept: the fleet agents of api, web read its board)" in said, said
    changelog = " ".join((Path(__file__).parents[1] / "CHANGELOG.md").read_text().split())
    assert (
        "`project forget` refuses it, naming every project that reads it, each with what frees "
        "it, and `project prune` keeps it, `--purge` included, naming those projects, unless"
    ) in changelog


@pytest.mark.parametrize("before", ["free", "its own agent"])
def test_a_prune_says_why_it_kept_a_row_as_the_run_read_it_not_the_plan(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch, before: str
) -> None:
    """Review of #259, round 34, 6/10: the table took the reason after "kept:" from the
    plan's copy of each row, and the summary below from what ``prune`` re-read. A hub
    beta's coder came to read while the confirmation sat open read "(kept: 0 live
    agent(s))", and one the plan held for its own agent, stopped since, said "1 live
    agent(s)", each beside a summary that named a hub. Both say what the run read."""
    from aisquare.services import project as project_service

    hub = _register(runner, monkeypatch, work_dir / "hub")
    beta = _register(runner, monkeypatch, work_dir / "beta")
    monkeypatch.chdir(work_dir)
    shutil.rmtree(work_dir / "hub")
    with store_session() as store:
        own = store.upsert_fleet_agent(_agent(hub, "manager")) if before != "free" else None
    real = project_service.prune_candidates

    def the_prompt_sat_open(**kwargs: Any) -> list[Any]:
        plan = real(**kwargs)
        with store_session() as store:
            if own is not None:
                store.end_fleet_agent(own.id, exit_status=0)
            store.upsert_fleet_agent(_agent(beta, "coder1").model_copy(update={"board": hub}))
        return plan

    monkeypatch.setattr(project_service, "prune_candidates", the_prompt_sat_open)

    result = runner.invoke(app, ["project", "prune", "--missing", "--yes"])

    assert result.exit_code == 0, result.output
    said = " ".join(result.stdout.split())
    assert "missing (kept: the fleet agents of beta" in said, said
    assert "live agent(s)" not in said, said
    assert "kept 1 whose board another project's fleet agents read, as a hub's" in said
    assert _listed(runner) == {hub, beta}


def test_a_prune_marks_only_what_it_dropped_and_says_a_row_gone_already(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 35, 4/8: a run marked ✓ every candidate it did not keep. One
    another shell forgot while the confirmation sat open is in neither ``dropped`` nor
    ``kept``, since ``prune`` passes over it, and it was marked ✓ too: three ✓ beside "✓
    forgot 2 registrations". ✓ is what the run dropped, · what it kept, and the row gone
    already is marked - and says so."""
    from aisquare.services import project as project_service

    ids = {name: _register(runner, monkeypatch, work_dir / name) for name in ("a", "b", "c")}
    monkeypatch.chdir(work_dir)
    for name in ids:
        shutil.rmtree(work_dir / name)
    real = project_service.prune_candidates

    def the_prompt_sat_open(**kwargs: Any) -> list[Any]:
        plan = real(**kwargs)
        assert {c.project.id for c in plan} == set(ids.values()), "the premise"
        with store_session() as store:
            store.forget_project(ids["b"])  # another shell's forget
        return plan

    monkeypatch.setattr(project_service, "prune_candidates", the_prompt_sat_open)

    result = runner.invoke(app, ["project", "prune", "--missing", "--yes"], env={"COLUMNS": "400"})

    assert result.exit_code == 0, result.output
    rows = {
        name: next(line.split() for line in result.stdout.splitlines() if project in line)
        for name, project in ids.items()
    }
    assert [rows[name][1] for name in ("a", "b", "c")] == ["✓", "-", "✓"], result.stdout
    assert "(gone already: forgotten since the plan)" in " ".join(result.stdout.split())
    assert "✓ forgot 2 registrations" in result.stdout


@pytest.mark.parametrize("elsewhere", ["forget", "purge"])
def test_a_purge_purges_a_row_forgotten_since_the_plan_and_says_one_purged_already(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch, elsewhere: str
) -> None:
    """Review of #259, round 36, 5/9: under ``--purge`` a candidate another shell forgot while
    the confirmation sat open was passed over as gone: "gone already: forgotten since the
    plan", beside "✓ purged 2 registrations", its rows and data directory kept, though their
    deletion was what ``--purge`` asked, and no later prune lists a forgotten row to purge.
    It is purged with the others; one purged already is said so."""
    from aisquare.services import project as project_service

    ids = {name: _register(runner, monkeypatch, work_dir / name) for name in ("a", "b", "c")}
    monkeypatch.chdir(work_dir)
    for name in ids:
        shutil.rmtree(work_dir / name)
    paths.project_data_dir(ids["b"]).mkdir(parents=True, exist_ok=True)
    real = project_service.prune_candidates

    def the_prompt_sat_open(**kwargs: Any) -> list[Any]:
        plan = real(**kwargs)
        with store_session() as store:
            getattr(store, f"{elsewhere}_project")(ids["b"])  # another shell's
        return plan

    monkeypatch.setattr(project_service, "prune_candidates", the_prompt_sat_open)

    result = runner.invoke(
        app, ["project", "prune", "--missing", "--purge", "--yes"], env={"COLUMNS": "400"}
    )

    assert result.exit_code == 0, result.output
    marker = next(line.split() for line in result.stdout.splitlines() if ids["b"] in line)[1]
    said = " ".join(result.stdout.split())
    with store_session() as store:
        assert store.get_project(ids["b"], include_forgotten=True) is None
    if elsewhere == "purge":
        assert marker == "-" and "(gone already: purged since the plan)" in said, said
        assert "✓ purged 2 registrations" in said, said
        return
    assert marker == "✓" and "gone already" not in said, result.stdout
    assert "✓ purged 3 registrations" in said, said
    assert not paths.project_data_dir(ids["b"]).exists()


@pytest.mark.parametrize("output", ["table", "json"])
def test_a_purge_says_a_row_forgotten_since_the_plan_that_is_stale_no_more_forgotten(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    """Review of #259, round 39, 4/9: under ``--purge`` the run re-reads a candidate another
    shell forgot while the confirmation sat open. One whose reason no longer held, its
    missing root back, was counted as changed: marked · and "kept: changed since the
    plan", and in "kept 1 that changed since the plan". Nothing kept it, and no list shows
    it. It is left forgotten, not purged, and said forgotten since the plan."""
    from aisquare.services import project as project_service

    ids = {name: _register(runner, monkeypatch, work_dir / name) for name in ("a", "b")}
    monkeypatch.chdir(work_dir)
    for name in ids:
        shutil.rmtree(work_dir / name)
    real = project_service.prune_candidates

    def the_prompt_sat_open(**kwargs: Any) -> list[Any]:
        plan = real(**kwargs)
        assert {c.project.id for c in plan} == set(ids.values()), "the premise"
        with store_session() as store:
            store.forget_project(ids["b"])  # another shell's forget
        (work_dir / "b").mkdir()  # and its root back
        return plan

    monkeypatch.setattr(project_service, "prune_candidates", the_prompt_sat_open)
    flags = ["--json"] if output == "json" else []

    result = runner.invoke(
        app,
        [*flags, "project", "prune", "--missing", "--purge", "--yes"],
        env={"COLUMNS": "400"},
    )

    assert result.exit_code == 0, result.output
    with store_session() as store:
        assert store.get_project(ids["b"]) is None
        assert store.get_project(ids["b"], include_forgotten=True) is not None, "not purged"
    if output == "json":
        report = _json(result.stdout)
        assert (report["dropped"], report["changed"], report["forgotten"]) == (
            [ids["a"]],
            [],
            [ids["b"]],
        )
        return
    marker = next(line.split() for line in result.stdout.splitlines() if ids["b"] in line)[1]
    said = " ".join(result.stdout.split())
    assert marker == "-" and "(gone already: forgotten since the plan)" in said, said
    assert "changed since the plan" not in said, said
    assert "✓ purged 1 registration" in said, said


@pytest.mark.parametrize("held", [False, True], ids=["its reader goes", "its reader stays"])
@pytest.mark.parametrize("hub_first", [True, False], ids=["hub first", "reader first"])
def test_a_prune_of_a_hub_and_its_reader_decides_every_hold_before_either_goes(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch, held: bool, hub_first: bool
) -> None:
    """Review of #259, round 34, 4/10: a prune asked each candidate's hold in turn and
    forgot as it went, and a stopped row holds a hub only while its project is listed to
    restart it. Hub and beta both ``--missing``, beta's one manager stopped on the hub's
    board: the hub first, it was kept and beta dropped, so nothing held it for the next
    prune; beta first, both went, though the plan said the hub was kept. Every hold is
    decided before anything goes, plan and run alike: a reader the prune drops holds
    nothing, and one it keeps (gamma's stopped row on beta's board holds beta) still holds."""
    from aisquare.services import project as project_service

    hub = _register(runner, monkeypatch, work_dir / "hub")
    beta = _register(runner, monkeypatch, work_dir / "beta")
    gamma = _register(runner, monkeypatch, work_dir / "gamma")
    monkeypatch.chdir(work_dir)
    shutil.rmtree(work_dir / "hub")
    shutil.rmtree(work_dir / "beta")
    with store_session() as store:
        reads = [(beta, hub), (gamma, beta)] if held else [(beta, hub)]
        for project_id, board in reads:
            row = store.upsert_fleet_agent(
                _agent(project_id, "manager").model_copy(update={"board": board})
            )
            store.end_fleet_agent(row.id, exit_status=0)

    plan = project_service.prune_candidates(missing=True, worktrees=False)
    by_id = {c.project.id: c for c in plan}
    assert set(by_id) == {hub, beta}
    readers = {p: [r.id for r in c.read_by] for p, c in by_id.items() if c.read_by}
    assert readers == ({hub: [beta], beta: [gamma]} if held else {}), "the plan"
    ordered = sorted(plan, key=lambda c: (c.project.id == hub) != hub_first)
    assert ordered[0].project.id == (hub if hub_first else beta)

    report = project_service.prune(ordered, purge=False, missing=True, worktrees=False)

    assert sorted(report.dropped) == ([] if held else sorted([hub, beta])), "as the plan said"
    assert {c.project.id: [r.id for r in c.read_by] for c in report.kept if c.read_by} == readers
    assert _listed(runner) == ({hub, beta, gamma} if held else {gamma})


def test_prune_moves_the_pin_when_it_drops_the_active_project(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha = _register(runner, monkeypatch, work_dir / "alpha")
    gone = _register(runner, monkeypatch, work_dir / "gone")
    assert runner.invoke(app, ["project", "switch", "gone"]).exit_code == 0
    monkeypatch.chdir(work_dir)
    shutil.rmtree(work_dir / "gone")
    assert pinned_project_id() == gone

    result = runner.invoke(app, ["project", "prune", "--missing", "--yes"])

    assert result.exit_code == 0, result.output
    assert "active project is now alpha" in result.stdout
    assert pinned_project_id() == alpha


def test_prune_purge_deletes_history_too(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = _register(runner, monkeypatch, work_dir / "gone")
    monkeypatch.chdir(work_dir)
    shutil.rmtree(work_dir / "gone")

    result = runner.invoke(app, ["project", "prune", "--missing", "--purge", "--yes"])

    assert result.exit_code == 0, result.output
    assert "purged 1 registration" in result.stdout
    assert _raw("SELECT COUNT(*) FROM entry WHERE project_id = ?", (gone,)) == [(0,)]
    assert _raw("SELECT COUNT(*) FROM project WHERE id = ?", (gone,)) == [(0,)]


# --- the companion defect ------------------------------------------------------


def test_json_project_list_carries_the_name_the_table_shows(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#83's companion: the table had a NAME column and the JSON had no such key."""
    _register(runner, monkeypatch, work_dir / "alpha")

    listed = runner.invoke(app, ["--json", "project", "list"])

    (project,) = _json(listed.stdout)
    assert project["name"] == "alpha"


def _corrupt_state() -> str:
    """Make `state.json` a JSON array — a file the shared writer refuses to touch."""
    body = '["was", "a", "list"]\n'
    paths.ensure_home()
    paths.state_path().write_text(body)
    return body


def test_forget_purge_completes_and_reports_a_pin_it_could_not_move(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_repin` ran inside the transaction: a `state.json` that refused the pin reported the
    command FAILED after the purge had committed and before the data directory was removed —
    orphaned for good, its registration gone — and a re-run said "no project matches"."""
    alpha = _register(runner, monkeypatch, work_dir / "alpha")
    beta = _register(runner, monkeypatch, work_dir / "beta")  # cwd is beta: active, unpinned
    data_dir = paths.project_data_dir(beta)
    (data_dir / "snapshot").mkdir(parents=True)
    body = _corrupt_state()

    result = runner.invoke(app, ["--json", "project", "forget", "beta", "--purge"])

    assert result.exit_code == 0, result.output
    report = _json(result.stdout)
    assert report["purged"] is True and report["data_dir_removed"] is True
    assert not data_dir.exists()
    assert report["active_changed"] is True and report["active"] is None
    assert "state.json" in report["pin_error"]
    assert paths.state_path().read_text() == body  # the user's file, untouched
    assert _listed(runner) == {alpha}


def test_forget_the_last_project_on_a_corrupt_state_file_is_complete(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unpinning what a corrupt file already does not pin is a no-op — `forget` of the last
    registration used to exit 1 with `state_unwritable` after tombstoning it."""
    _register(runner, monkeypatch, work_dir / "alpha")
    body = _corrupt_state()

    result = runner.invoke(app, ["project", "forget", "alpha"])

    assert result.exit_code == 0, result.output
    assert "follows your working directory" in result.stdout and "⚠" not in result.stdout
    assert paths.state_path().read_text() == body
    assert "No projects registered yet" in runner.invoke(app, ["project", "list"]).stdout


def test_forget_purge_of_the_last_project_reports_a_state_file_it_can_no_longer_read(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of the #167 fold, F6. With no project left, `_repin` unpins, and the unpin asks
    `pinned_project_id` first — a strict read, whose `PermissionError` is not the
    `StateUnwritableError` `_repin` catches. A `state.json` that became unreadable after
    `forget` had resolved the active project escaped the committed purge as a traceback, and
    the data directory was never removed. The refusal names the file, as a write's does."""
    alpha = _register(runner, monkeypatch, work_dir / "alpha")  # the cwd's: active, unpinned
    data_dir = paths.project_data_dir(alpha)
    (data_dir / "snapshot").mkdir(parents=True)
    real_purge = SqliteStore.purge_project
    unreadable = False

    def purge(self: SqliteStore, project_id: str) -> dict[str, int]:
        nonlocal unreadable
        removed = real_purge(self, project_id)
        unreadable = True  # from here on, as if the file's mode had just changed
        return removed

    def read(*, strict: bool = False) -> dict[str, object]:
        if unreadable and strict:
            raise PermissionError(13, "Permission denied", str(paths.state_path()))
        return read_state(strict=strict)

    monkeypatch.setattr(SqliteStore, "purge_project", purge)
    monkeypatch.setattr("aisquare.core.workspace.read_state", read)

    result = runner.invoke(app, ["--json", "project", "forget", "alpha", "--purge"])

    assert result.exit_code == 0, result.output
    report = _json(result.stdout)
    assert report["purged"] is True and report["data_dir_removed"] is True
    assert not data_dir.exists()
    assert report["active_changed"] is True and report["active"] is None
    assert "state.json could not be read" in report["pin_error"], report["pin_error"]
    assert "Permission denied" in report["pin_error"]


def test_prune_reports_a_pin_it_could_not_move_instead_of_a_traceback(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, repo_and_worktree: tuple[Path, Path]
) -> None:
    """`prune` reached the same `pin_project` with no handler: a raw traceback, and under
    `--json` no error envelope at all.

    The pinned project has to be the one dropped, and a corrupt file cannot hold a
    pin, so the refusal is the writer's own (its lock held by another process,
    say) while the file still names the worktree.
    """
    repo, worktree = repo_and_worktree
    wt_id = _register_worktree_as_its_own_project(worktree)
    principal = _register(runner, monkeypatch, repo)
    assert runner.invoke(app, ["project", "switch", wt_id]).exit_code == 0
    assert pinned_project_id() == wt_id
    body = paths.state_path().read_text()

    def refuse(key: str, value: object) -> None:
        raise StateUnwritableError(f"{paths.state_path()}.lock is held by another process")

    monkeypatch.setattr("aisquare.core.workspace.update_state", refuse)

    result = runner.invoke(app, ["project", "prune", "--worktrees", "--yes"])

    assert result.exit_code == 0, result.output
    assert "✓ forgot 1 registration" in result.stdout
    assert "⚠ the pin could not be moved" in result.stdout and "state.json" in result.stdout
    assert _listed(runner) == {principal}
    assert paths.state_path().read_text() == body, "left as it was — still naming the worktree"


# --- captured versus onboarded (#139) ------------------------------------------------------


def _capture(runner: CliRunner, monkeypatch: pytest.MonkeyPatch, directory: Path) -> str:
    """Register ``directory`` the way a hooked session does: a prompt, nothing else."""
    directory.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"session_id": f"ses_{directory.name}", "cwd": str(directory), "prompt": "hi"}
    )
    result = runner.invoke(app, ["hook", "user-prompt-submit"], input=payload)
    assert result.exit_code == 0, result.output
    return project_id_for(directory.resolve())


def test_a_hooked_session_captures_a_directory_without_listing_it(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, project_capture: None
) -> None:
    shown = _register(runner, monkeypatch, tmp_path / "shown")  # a fact by hand: on purpose
    scratch = _capture(runner, monkeypatch, tmp_path / "scratch")

    assert _listed(runner) == {shown}
    everything = runner.invoke(app, ["--json", "project", "list", "--all"])
    rows = {row["id"]: row for row in _json(everything.stdout)}
    assert set(rows) == {shown, scratch}
    assert rows[scratch]["onboarded_at"] is None and rows[shown]["onboarded_at"] is not None
    table = runner.invoke(app, ["project", "list", "--all"])
    assert "LISTED" in table.stdout and "captured" in table.stdout
    plain = runner.invoke(app, ["project", "list"])
    assert "LISTED" not in plain.stdout, "the everyday table is unchanged"
    # The captured directory still works for what capture is for: its history is there.
    assert _raw("SELECT COUNT(*) FROM prompt WHERE project_id = ?", (scratch,)) == [(1,)]
    # Adding it on purpose lists it.
    monkeypatch.chdir(tmp_path / "scratch")
    onboarded = runner.invoke(app, ["project", "onboard"])
    assert onboarded.exit_code == 0, onboarded.output
    assert _listed(runner) == {shown, scratch}


def test_forget_sticks_against_the_next_prompt(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, project_capture: None
) -> None:
    """The issue's complaint: `ensure_project` cleared `forgotten_at`, so the next
    prompt in a forgotten directory put it back on the list. It now comes back
    CAPTURED — not listed, but not a tombstone the prompt vanishes into either."""
    project_id = _register(runner, monkeypatch, tmp_path / "gone")
    assert runner.invoke(app, ["project", "forget", project_id[:12]]).exit_code == 0
    assert _capture(runner, monkeypatch, tmp_path / "gone") == project_id
    assert project_id not in _listed(runner)
    everything = runner.invoke(app, ["--json", "project", "list", "--all"])
    rows = {row["id"]: row for row in _json(everything.stdout)}
    assert project_id in rows and rows[project_id]["onboarded_at"] is None, "captured again"
    # A deliberate add lists it again, history and all.
    monkeypatch.chdir(tmp_path / "gone")
    assert runner.invoke(app, ["context", "add", "back", "--project"]).exit_code == 0
    assert project_id in _listed(runner)


def test_a_pruned_capture_is_captured_again_by_the_next_prompt(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, project_capture: None
) -> None:
    """A forgotten row that a later prompt could not revive was a permanent, invisible
    tombstone: the prompt was written into it, `log` said nothing had been captured,
    and `--all`, `doctor` and the next prune never saw the directory again."""
    scratch = _capture(runner, monkeypatch, tmp_path / "scratch")
    pruned = runner.invoke(
        app, ["--json", "project", "prune", "--captured-only", "--older-than", "0", "--yes"]
    )
    assert set(_json(pruned.stdout)["dropped"]) == {scratch}

    assert _capture(runner, monkeypatch, tmp_path / "scratch") == scratch
    everything = runner.invoke(app, ["--json", "project", "list", "--all"])
    assert scratch in {row["id"] for row in _json(everything.stdout)}
    assert scratch not in _listed(runner), "captured, so still not listed"
    monkeypatch.chdir(tmp_path / "scratch")
    logged = runner.invoke(app, ["--json", "log"])
    assert logged.exit_code == 0, logged.output
    assert [prompt["text"] for prompt in _json(logged.stdout)] == ["hi", "hi"]
    again = runner.invoke(
        app, ["--json", "project", "prune", "--captured-only", "--older-than", "0", "--yes"]
    )
    assert set(_json(again.stdout)["dropped"]) == {scratch}, "the next prune finds it again"


def test_prune_captured_only_drops_stale_captures_and_keeps_the_rest(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, project_capture: None
) -> None:
    shown = _register(runner, monkeypatch, tmp_path / "shown")
    fresh = _capture(runner, monkeypatch, tmp_path / "fresh")
    stale = _capture(runner, monkeypatch, tmp_path / "stale")
    with_fact = _capture(runner, monkeypatch, tmp_path / "with-fact")
    long_ago = "2020-01-01T00:00:00+00:00"
    for project_id in (stale, with_fact):
        _raw_write("UPDATE project SET created_at = ? WHERE id = ?", (long_ago, project_id))
        _raw_write("UPDATE prompt SET created_at = ? WHERE project_id = ?", (long_ago, project_id))
    _raw_write(
        "INSERT INTO entry (id, pool, project_id, text, tags, source, created_at, updated_at) "
        "VALUES ('ent_keep', 'project', ?, 'kept fact', '[]', 'cli', ?, ?)",
        (with_fact, long_ago, long_ago),
    )

    plan = runner.invoke(app, ["--json", "project", "prune", "--captured-only"])
    assert plan.exit_code == 0, plan.output
    planned = {c["project"]["id"]: c["reason"] for c in _json(plan.stdout)["candidates"]}
    assert planned == {stale: "captured"}, planned  # fresh: too recent; with-fact: has a fact

    everything = runner.invoke(
        app, ["--json", "project", "prune", "--captured-only", "--older-than", "0", "--yes"]
    )
    assert everything.exit_code == 0, everything.output
    dropped = set(_json(everything.stdout)["dropped"])
    assert dropped == {stale, fresh}, "with --older-than 0 every fact-less capture goes"
    listing = runner.invoke(app, ["--json", "project", "list", "--all"])
    remaining = {row["id"] for row in _json(listing.stdout)}
    assert remaining == {shown, with_fact}
    assert _listed(runner) == {shown}


@pytest.mark.parametrize("purge", [False, True], ids=["forget", "purge"])
@pytest.mark.parametrize("since", ["onboarded", "an entry", "a prompt"])
def test_a_prune_keeps_a_capture_that_stopped_being_stale_while_the_prompt_sat_open(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    project_capture: None,
    purge: bool,
    since: str,
) -> None:
    """Review of #259, round 37, 2/7: ``prune`` re-read what holds each candidate, as the
    prompt may have sat open, but not why it was one. A capture added on purpose there
    meanwhile (``project onboard``), or given an entry by a session, was forgotten anyway,
    and under ``--purge`` the entry just saved went with it, and its data directory. Each
    reason is re-read in the run's store: such a row is kept, as changed since the plan,
    and so is one touched since, by the ``--older-than`` the plan was made with."""
    from aisquare.core.entries import new_entry
    from aisquare.services import project as project_service

    scratch = _capture(runner, monkeypatch, tmp_path / "scratch")
    other = _capture(runner, monkeypatch, tmp_path / "other")
    long_ago = "2020-01-01T00:00:00+00:00"
    for project_id in (scratch, other):
        _raw_write("UPDATE project SET created_at = ? WHERE id = ?", (long_ago, project_id))
        _raw_write("UPDATE prompt SET created_at = ? WHERE project_id = ?", (long_ago, project_id))
    paths.project_data_dir(scratch).mkdir(parents=True, exist_ok=True)
    real = project_service.prune_candidates

    def the_prompt_sat_open(**kwargs: Any) -> list[Any]:
        plan = real(**kwargs)
        assert {c.project.id: c.reason for c in plan} == {
            scratch: "captured",
            other: "captured",
        }, "the premise"
        with store_session() as store:
            if since == "onboarded":
                row = store.get_project(scratch)
                assert row is not None
                store.onboard_project(row)
            elif since == "an entry":
                store.add(new_entry("saved meanwhile", "project", scratch, [], "cli"))
            else:
                store.add_prompt("a prompt meanwhile", scratch)
        return plan

    monkeypatch.setattr(project_service, "prune_candidates", the_prompt_sat_open)
    flags = ["--purge"] if purge else []

    result = runner.invoke(app, ["--json", "project", "prune", "--captured-only", *flags, "--yes"])

    assert result.exit_code == 0, result.output
    report = _json(result.stdout)
    assert (report["dropped"], report["changed"], report["kept"]) == ([other], [scratch], [])
    with store_session() as store:
        assert store.get_project(scratch) is not None, "kept, not forgotten"
        assert store.get_project(other, include_forgotten=purge) is None, "the other goes"
        texts = [entry.text for entry in store.entries("project", project_id=scratch)]
    assert texts == (["saved meanwhile"] if since == "an entry" else [])
    assert paths.project_data_dir(scratch).is_dir(), "its data directory stays"


def test_a_prune_keeps_a_missing_root_that_came_back_and_says_so(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 37, 2/7: a missing root back on disk while the confirmation sat
    open, a network mount remounted, was forgotten all the same. It is kept, marked · and
    said changed since the plan, and the summary counts it."""
    from aisquare.services import project as project_service

    ids = {name: _register(runner, monkeypatch, work_dir / name) for name in ("a", "b")}
    monkeypatch.chdir(work_dir)
    for name in ids:
        shutil.rmtree(work_dir / name)
    real = project_service.prune_candidates

    def the_prompt_sat_open(**kwargs: Any) -> list[Any]:
        plan = real(**kwargs)
        assert {c.project.id for c in plan} == set(ids.values()), "the premise"
        (work_dir / "a").mkdir()  # remounted
        return plan

    monkeypatch.setattr(project_service, "prune_candidates", the_prompt_sat_open)

    result = runner.invoke(app, ["project", "prune", "--missing", "--yes"], env={"COLUMNS": "400"})

    assert result.exit_code == 0, result.output
    rows = {
        name: next(line.split() for line in result.stdout.splitlines() if project in line)
        for name, project in ids.items()
    }
    assert [rows[name][1] for name in ("a", "b")] == ["·", "✓"], result.stdout
    said = " ".join(result.stdout.split())
    assert "missing (kept: changed since the plan)" in said, said
    assert "✓ forgot 1 registration" in said and "kept 1 that changed since the plan" in said
    assert _listed(runner) == {ids["a"]}


@pytest.mark.parametrize(
    ("flags", "dropped"), [([], True), (["--worktrees"], False)], ids=["plain", "worktrees-only"]
)
def test_a_prune_drops_a_worktree_whose_tree_went_while_the_prompt_sat_open(
    runner: CliRunner,
    repo_and_worktree: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    flags: list[str],
    dropped: bool,
) -> None:
    """Review of #259, round 38, 2/4: a worktree candidate read as changed whenever its root
    named no principal, as a root that is gone names none: a tree a ``fleet reap`` removed
    while the confirmation sat open was kept as "no longer stale" by a plain prune, which
    asks for missing roots too. It is dropped; asked for worktrees alone, it is kept, as a
    plan made then would not list it."""
    from aisquare.services import project as project_service

    repo, worktree = repo_and_worktree
    wt_id = _register_worktree_as_its_own_project(worktree)
    repo_id = _register(runner, monkeypatch, repo)
    real = project_service.prune_candidates

    def the_prompt_sat_open(**kwargs: Any) -> list[Any]:
        plan = real(**kwargs)
        assert [(c.project.id, c.reason) for c in plan] == [(wt_id, "worktree")], "the premise"
        shutil.rmtree(worktree)  # reaped
        return plan

    monkeypatch.setattr(project_service, "prune_candidates", the_prompt_sat_open)

    result = runner.invoke(app, ["--json", "project", "prune", *flags, "--yes"])

    assert result.exit_code == 0, result.output
    report = _json(result.stdout)
    assert (report["dropped"], report["changed"]) == (([wt_id], []) if dropped else ([], [wt_id]))
    assert _listed(runner) == ({repo_id} if dropped else {repo_id, wt_id})


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("tree gone", "missing"),
        ("root back as a worktree", "worktree"),
        ("principal forgotten", None),
        ("capture added, root gone", "missing"),
    ],
)
def test_a_prunes_run_reads_each_candidate_by_the_plans_own_rules(
    runner: CliRunner,
    repo_and_worktree: tuple[Path, Path],
    work_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    project_capture: None,
    case: str,
    reason: str | None,
) -> None:
    """Review of #259, round 38, 4/4: ``prune`` re-read each candidate by its reason's rule
    written out again, inverted, beside the plan's, and the copies drifted (2/4 was the
    first). A missing root back as a worktree of a registered repo was kept as changed,
    though a plain prune drops worktrees too, and a capture added on purpose whose root went
    was kept by ``--captured-only --missing``. One set of rules reads both: the run drops what
    a plan made at that moment would, keeps as changed what it would not list, and gives
    each row it drops the reason that plan would."""
    from aisquare.services import project as project_service

    repo, worktree = repo_and_worktree
    repo_id = _register(runner, monkeypatch, repo)
    back, scratch = work_dir / "back", work_dir / "scratch"
    flags: list[str] = []
    if case in ("tree gone", "principal forgotten"):
        target = _register_worktree_as_its_own_project(worktree)
    elif case == "root back as a worktree":
        target = _register(runner, monkeypatch, back)
        monkeypatch.chdir(work_dir)
        shutil.rmtree(back)
    else:
        target = _capture(runner, monkeypatch, scratch)
        monkeypatch.chdir(work_dir)
        flags = ["--captured-only", "--missing", "--older-than", "0"]
    real = project_service.prune_candidates
    now: list[Any] = []

    def the_prompt_sat_open(**kwargs: Any) -> list[Any]:
        plan = real(**kwargs)
        assert [c.project.id for c in plan] == [target], "the premise"
        if case == "tree gone":
            shutil.rmtree(worktree)  # reaped
        elif case == "root back as a worktree":
            _git("worktree", "add", "-q", str(back), "-b", "back", cwd=repo)
        elif case == "principal forgotten":
            with store_session() as store:
                store.forget_project(repo_id)
        else:
            with store_session() as store:
                row = store.get_project(target)
                assert row is not None
                store.onboard_project(row)
            shutil.rmtree(scratch)
        now.extend(real(**kwargs))  # a plan made now
        return plan

    monkeypatch.setattr(project_service, "prune_candidates", the_prompt_sat_open)

    result = runner.invoke(app, ["--json", "project", "prune", *flags, "--yes"])

    assert result.exit_code == 0, result.output
    report = _json(result.stdout)
    planned_now = {c.project.id: c.reason for c in now if not c.held}
    assert planned_now == ({} if reason is None else {target: reason}), "the case"
    assert report["dropped"] == list(planned_now), report
    assert report["changed"] == ([] if planned_now else [target]), report
    given = {c["project"]["id"]: c["reason"] for c in report["candidates"]}
    assert {project_id: given[project_id] for project_id in report["dropped"]} == planned_now


def test_an_empty_list_names_the_captured_directories_it_hides(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, project_capture: None
) -> None:
    """With nothing but captures on the machine, `project list` said "No projects
    registered yet. Run: aisquare init" and `status` "0 project(s) registered" —
    neither said a word about the directories they hide."""
    _capture(runner, monkeypatch, tmp_path / "scratch")

    listed = " ".join(runner.invoke(app, ["project", "list"]).stdout.split())
    assert "No projects registered yet" not in listed
    assert "1 captured directory hidden" in listed and "aisquare project list --all" in listed
    status = " ".join(runner.invoke(app, ["status"]).stdout.split())
    assert "0 project(s) registered (+1 captured, hidden" in status
    assert _json(runner.invoke(app, ["--json", "status"]).stdout)["captured_count"] == 1


def test_switching_to_a_captured_directory_lists_it(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, project_capture: None
) -> None:
    """Pinning a directory is choosing it: the active project must not be the one
    `project list` (no `*` row) and the sidebar leave out."""
    scratch = _capture(runner, monkeypatch, tmp_path / "scratch")
    assert scratch not in _listed(runner)

    switched = runner.invoke(app, ["project", "switch", "scratch"])

    assert switched.exit_code == 0, switched.output
    assert scratch in _listed(runner)
    assert "│ * │ scratch" in runner.invoke(app, ["project", "list"]).stdout  # the active row


def test_older_than_without_captured_only_is_refused(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    """Ignored silently, `--older-than 7` read as "what is older than a week" and the
    command swept every missing root instead."""
    gone = _register(runner, monkeypatch, work_dir / "gone")
    monkeypatch.chdir(work_dir)
    shutil.rmtree(work_dir / "gone")

    refused = runner.invoke(app, ["project", "prune", "--older-than", "7", "--yes"])
    as_json = runner.invoke(app, ["--json", "project", "prune", "--older-than", "7", "--yes"])

    assert refused.exit_code == 1 and "applies only with --captured-only" in refused.output
    assert as_json.exit_code == 1 and _json(as_json.stdout)["error"] == "usage"
    assert gone in _listed(runner), "nothing was dropped"


@pytest.mark.parametrize("folder", ["My Repo", "repo-$HOME"], ids=["space", "dollar"])
def test_a_busy_project_names_the_shutdown_to_run_by_its_id_without_a_codename(
    tmp_path: Path, folder: str
) -> None:
    """With no codename yet, a busy project's refusal named `fleet shutdown --project` with
    its folder's name bare: a space split it, and a `$` was expanded by the shell when
    pasted (review of #257). Then with its nickname, which another project's folder could
    take (below). It names the id, quoted for this shell, which reads back as that one ref;
    the prose names the folder."""
    from aisquare.services import install_route
    from aisquare.services import project as project_service

    project = ProjectInfo(id="p-1", root=tmp_path / folder)
    agent = FleetAgent(
        id="a-1",
        project_id=project.id,
        label="coder-a",
        role="coder",
        pane_id="%1",
        cwd=project.root,
        created_at=datetime.now(UTC),
    )
    said = str(project_service.ProjectBusyError(project, [agent]))
    printed = said.split("gone too, ", 1)[1].removesuffix(" records them")

    argv = ["aisquare", "fleet", "shutdown", "--project", "p-1"]
    assert printed == install_route.command_line(argv), said
    assert install_route.split_line(printed) == argv, said
    assert said.startswith(f"{folder} has 1 live fleet agent(s)"), said


def test_a_busy_nicknamed_projects_shutdown_is_never_another_projects(work_dir: Path) -> None:
    """`/work/api` nicknamed "web", and `/work/web` registered too: forgetting `api` was
    refused with `fleet shutdown --project web`, which `find_projects` reads as `/work/web`
    (a nickname matches last), and running it shut down that project's fleet (review of
    #259, round 13). The command names the project it refused."""
    from aisquare.services import fleet as fleet_service
    from aisquare.services import install_route
    from aisquare.services import project as project_service

    roots = [work_dir / "api", work_dir / "web"]
    with store_session() as store:
        api, web = (
            store.onboard_project(ProjectInfo(id=project_id_for(root), root=root, linked_repos=[]))
            for root in roots
        )
    api = project_service.set_nickname(api, "web")
    agent = FleetAgent(
        id="a-1", project_id=api.id, label="coder-a", role="coder", pane_id="%1",
        cwd=api.root, created_at=datetime.now(UTC),
    )  # fmt: skip

    said = str(project_service.ProjectBusyError(api, [agent]))
    printed = said.split("gone too, ", 1)[1].removesuffix(" records them")
    ref = install_route.split_line(printed)[-1]

    assert said.startswith("web has 1 live fleet agent(s)"), said  # the prose: its nickname
    assert fleet_service.resolve_project(ref).id == api.id != web.id
    # The stop and the reap it names too: alone, each acted on the active project (round 19).
    stop = said.split("stop them first (", 1)[1].split(" <label>)", 1)[0]
    reap = said.split("or run ", 1)[1].split(" if they", 1)[0]
    for command in (stop, reap):
        argv = install_route.split_line(command)
        assert argv[-2] == "--project" and fleet_service.resolve_project(argv[-1]).id == api.id
