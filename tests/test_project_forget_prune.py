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
from datetime import UTC, datetime
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

    with store_session() as store:  # the store's own purge: made-up rows are no models
        removed = store.purge_project(alpha)

    assert removed["project"] == 1
    for table, columns in pointing.items():
        for column in columns:
            count = f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" = ?'
            assert _raw(count, (alpha,)) == [(0,)], f"{table}.{column} kept the purged project"
            assert _raw(count, (beta,)) != [(0,)], f"{table}.{column} lost the bystander's row"
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
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
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
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
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
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
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
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
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


def test_an_empty_list_names_the_captured_directories_it_hides(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
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
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
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
