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

import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import paths
from aisquare.core.ids import new_agent_id, new_event_id, new_task_id
from aisquare.core.store import store_session
from aisquare.core.workspace import pinned_project_id, project_id_for, worktree_principal
from aisquare.models import FleetAgent, ProjectInfo, TeamEvent, TeamSession, TeamTask
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
        "team_meta": 3,
        "project": 1,
    }
    for table in ("entry", "prompt", "team_session", "team_task", "team_event", "fleet_agent"):
        assert _raw(f"SELECT COUNT(*) FROM {table} WHERE project_id = ?", (alpha,)) == [(0,)]
    assert _raw("SELECT COUNT(*) FROM project WHERE id = ?", (alpha,)) == [(0,)]
    assert not data_dir.exists()
    # The bystander kept everything.
    assert _listed(runner) == {beta}
    assert _raw("SELECT COUNT(*) FROM entry WHERE project_id = ?", (beta,)) == [(1,)]
    assert _raw("SELECT value FROM team_meta WHERE key = ?", (f"distill_seq:{beta}",)) == [("7",)]


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
        removed = store.purge_project(alpha)
    assert removed["entry"] == 1 and removed["project"] == 1


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
    assert worktree_principal(orphan) == Path("/nowhere/repo")
    bare = tmp_path / "of-bare"
    bare.mkdir()
    (bare / ".git").write_text("gitdir: /srv/repo.git/worktrees/x\n", encoding="utf-8")
    assert worktree_principal(bare) == Path("/srv/repo.git")


def _register_worktree_as_its_own_project(worktree: Path) -> str:
    """The registration shape the issue measured: a worktree with its OWN row.

    Registering through the CLI cannot produce it any more — a worktree resolves
    to its principal — so the row is written directly, as whatever made the 305
    of them did.
    """
    root = worktree.resolve()
    with store_session() as store:
        store.ensure_project(ProjectInfo(id=project_id_for(root), root=root, linked_repos=[]))
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
