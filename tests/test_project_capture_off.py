"""``[projects] capture`` off, the default since 0.8.1: no project row unless one is added on
purpose (#139) — the board, a destination and doctor.

The rest of the call sites are tested beside their own: the hooks in tests/test_hooks.py
and tests/test_ci_augment.py, the shell in tests/test_ui_shell.py, the fleet in
tests/test_fleet_service.py, the MCP server in tests/test_serve.py.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.entries import new_entry
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, ProjectInfo
from aisquare.services import destinations as dest
from aisquare.services import diagnostics
from aisquare.services import team as team_service


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


def _rows() -> list[ProjectInfo]:
    with store_session() as store:
        return store.list_projects(all=True, include_forgotten=True)


# --- the board -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["note", "hello"],
        ["task", "add", "wire auth"],
        ["task", "next", "--claim"],
        ["team", "signal", "phase", "build"],
    ],
    ids=["note", "task-add", "task-claim", "signal"],
)
def test_a_board_write_where_nothing_was_added_is_refused_and_adds_nothing(
    runner: CliRunner, work_dir: Path, argv: list[str]
) -> None:
    result = runner.invoke(app, ["--json", *argv])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["error"] == "not_added" and payload["ref"] == str(work_dir.resolve())
    assert f"add it first: aisquare project onboard {work_dir.resolve()}" in payload["detail"]
    assert _rows() == [], "a refusal writes no row"


def test_the_refusal_says_what_to_run_where_a_person_reads_it(
    runner: CliRunner, work_dir: Path
) -> None:
    result = runner.invoke(app, ["note", "hello"])
    assert result.exit_code == 1
    assert "repo is not one of your projects — add it first: aisquare project onboard" in (
        result.output
    )


def test_the_refusal_quotes_the_onboard_for_this_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """The onboard the refusal names was ``shlex.quote``d: on Windows a path came out in
    single quotes, which cmd.exe passes on as part of the path, and the refusal tests failed
    on every windows-latest run (CI gate of #259). It is quoted for this shell, and reads
    back as the one command."""
    import sys

    from aisquare.services import install_route
    from aisquare.services import project as project_service

    monkeypatch.setattr(sys, "platform", "win32")
    root = Path("C:/Users/me/My Repo")
    said = str(project_service.NotAdded(ProjectInfo(id="prj_x", root=root)))
    onboard = said.split("add it first: ", 1)[1]

    assert install_route.split_line(onboard) == ["aisquare", "project", "onboard", str(root)]


@pytest.mark.parametrize(
    "root",
    [r"C:\Users\jo\$scratch", r"C:\Users\jo\`tmp", r"C:\Users\%USERNAME%\work", r"C:\it's $x"],
    ids=["dollar", "backtick", "percent", "quote-and-dollar"],
)
def test_a_windows_path_a_shell_would_expand_is_quoted_for_powershell(
    monkeypatch: pytest.MonkeyPatch, root: str
) -> None:
    """Review of #259, round 21: on Windows a word was double-quoted, but PowerShell, the
    default shell there, expands ``$name`` and backtick escapes inside double quotes, and
    cmd.exe expands ``%NAME%``. So ``project onboard "C:\\Users\\jo\\$scratch"`` onboarded
    ``C:\\Users\\jo\\``. Such a word is now in PowerShell's single quotes, which it reads
    literally (cmd.exe passes them on, so there the command fails rather than acting on
    another directory), and it reads back as the one command."""
    import sys

    from aisquare.services import install_route
    from aisquare.services import project as project_service

    monkeypatch.setattr(sys, "platform", "win32")
    onboard = project_service.onboard_command(Path(root))

    word = onboard.removeprefix("aisquare project onboard ")
    assert word == "'" + root.replace("'", "''") + "'", onboard  # nothing PowerShell expands
    assert install_route.split_line(onboard) == ["aisquare", "project", "onboard", root]
    # A word with none of them keeps the double quotes both shells read literally.
    plain = install_route.command_line(["aisquare", "project", "onboard", r"C:\My Repo"])
    assert plain == r'aisquare project onboard "C:\My Repo"'
    with pytest.raises(ValueError, match="no closing quotation"):
        install_route.split_line("aisquare project onboard 'C:\\x")


def test_a_board_read_where_nothing_was_added_is_empty_and_adds_nothing(
    runner: CliRunner,
) -> None:
    for argv in (["board"], ["task", "list"], ["log"], ["task", "next"]):
        result = runner.invoke(app, ["--json", *argv])
        assert result.exit_code == 0, (argv, result.output)
    assert _rows() == []


def test_team_on_adds_the_project_and_the_board_takes_writes(
    runner: CliRunner, work_dir: Path
) -> None:
    assert runner.invoke(app, ["team", "on"]).exit_code == 0
    assert runner.invoke(app, ["note", "hello"]).exit_code == 0
    with store_session() as store:
        [added] = store.list_projects()
    assert added.id == team_project(work_dir).id


def test_with_capture_on_a_board_write_captures_the_directory_unlisted(
    runner: CliRunner, work_dir: Path, project_capture: None
) -> None:
    assert runner.invoke(app, ["note", "hello"]).exit_code == 0
    with store_session() as store:
        assert [p.id for p in store.captured_projects()] == [team_project(work_dir).id]
        assert store.list_projects() == []


def test_a_role_session_where_nothing_was_added_joins_no_board(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``AISQUARE_ROLE`` activates a board, and still adds nothing: ``launch`` adds its
    project on purpose (``team on``); a bare ``AISQUARE_ROLE=coder claude`` does not."""
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    session = "dddd4444-0000-0000-0000-000000000000"
    payload = {"cwd": str(work_dir), "session_id": session}

    start = runner.invoke(app, ["hook", "session-start"], input=json.dumps(payload))
    prompt = runner.invoke(
        app, ["hook", "user-prompt-submit"], input=json.dumps({**payload, "prompt": "go"})
    )

    assert start.exit_code == 0 and start.stdout == ""
    assert prompt.exit_code == 0 and prompt.stdout == ""
    assert _rows() == []
    with store_session() as store:
        assert store.get_session(session) is None
    team_service.activate()  # what `aisquare launch coder` does first
    start = runner.invoke(app, ["hook", "session-start"], input=json.dumps(payload))
    assert "role: coder" in start.stdout


def test_a_session_start_with_no_board_to_join_writes_nothing(
    work_dir: Path, project_capture: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 18: with capture on, the team hook captured the directory (an
    upsert, the store's write lock) before it asked whether a board was active or a role
    set, then returned with nothing to write. A session start that joins no board writes
    nothing; one with a role captures the directory for its session row."""
    session = "eeee5555-0000-0000-0000-000000000000"

    assert team_service.hook_session_start(session, work_dir, "startup") == ""
    assert _rows() == []

    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    assert "role: coder" in team_service.hook_session_start(session, work_dir, "startup")
    assert [row.onboarded_at for row in _rows()] == [None], "captured, not added"
    with store_session() as store:
        assert store.get_session(session) is not None


def test_a_prompt_with_no_board_to_join_writes_no_project(
    work_dir: Path, project_capture: None
) -> None:
    """Review of #259, round 19: the prompt's team hook still captured the directory before
    it asked whether a board was active or a role set, as the session start's did until
    round 18: with capture on, every prompt of a session on no board took the store's write
    lock a second time. It captures only for a session row it writes."""
    session = "ffff6666-0000-0000-0000-000000000000"

    assert team_service.hook_prompt_heartbeat(session, work_dir) == ""
    assert _rows() == []


def test_team_spawn_adds_its_project_when_it_starts_the_session_not_when_it_prints(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--exec`` is ``launch`` by another door: the session it starts joins this board,
    so it adds the project as ``launch`` does. A printed line may never be run."""
    monkeypatch.setattr("aisquare.cli.team.shutil.which", lambda _name: "/usr/bin/claude")
    monkeypatch.setattr("aisquare.cli.team.os.execvpe", lambda *_args: None)

    printed = runner.invoke(app, ["team", "spawn", "coder", "--no-probe"])
    assert printed.exit_code == 0, printed.output
    assert _rows() == []

    started = runner.invoke(app, ["team", "spawn", "coder", "--no-probe", "--exec"])
    assert started.exit_code == 0, started.output
    with store_session() as store:
        assert [p.id for p in store.list_projects()] == [team_project(work_dir).id]


@pytest.mark.parametrize("json_output", [False, True], ids=["text", "json"])
def test_team_spawn_says_when_the_line_it_prints_joins_no_board(
    runner: CliRunner, work_dir: Path, json_output: bool
) -> None:
    """Review of #259, round 17: a role session's hooks join only a project someone added,
    and a printed line adds nothing. Pasted in a directory nobody added, it started a
    planner with no board, no tasks and no protocol, and nothing said why. The print says
    so, with the command that adds it; once added, it says nothing."""
    flags = ["--json"] if json_output else []
    argv = [*flags, "team", "spawn", "planner", "--no-probe"]

    printed = runner.invoke(app, argv)

    assert printed.exit_code == 0, printed.output
    said = json.loads(printed.stdout)["not_added"] if json_output else printed.stderr
    said = " ".join(said.split())
    onboard = f"aisquare project onboard {work_dir.resolve()}"
    assert f"repo is not one of your projects — add it first: {onboard}" in said
    assert "joins no board" in said
    assert _rows() == [], "saying so adds nothing"
    assert runner.invoke(app, ["team", "on"]).exit_code == 0
    again = runner.invoke(app, argv)
    assert again.exit_code == 0 and "joins no board" not in again.stderr
    if json_output:
        assert json.loads(again.stdout)["not_added"] is None


# --- a destination ---------------------------------------------------------------------


def test_a_destination_where_nothing_was_added_is_refused_before_any_sign_in(
    runner: CliRunner, work_dir: Path
) -> None:
    """``explainability use`` names the destination of a project; one nobody added is
    refused first — no sign-in asked for, no row written."""
    result = runner.invoke(app, ["--json", "explainability", "use", "acme/Frontend"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["error"] == "not_added"
    assert "aisquare project onboard" in payload["detail"]
    assert _rows() == []


def test_a_destination_needs_the_project_added_or_captured(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.services import project as project_service

    project = team_project(work_dir)
    with store_session() as store:
        with pytest.raises(dest.DestinationError) as refused:
            dest.require_project(store, project)
        assert refused.value.code == "not_added"
        monkeypatch.setattr(project_service, "capture_on", lambda: True)
        dest.require_project(store, project)  # captured, as 0.8.0 did
        assert [p.id for p in store.captured_projects()] == [project.id]
        monkeypatch.setattr(project_service, "capture_on", lambda: False)
        store.onboard_project(project)
        dest.require_project(store, project)


# --- doctor ----------------------------------------------------------------------------


def test_doctor_names_the_prune_that_clears_what_capture_left(tmp_path: Path) -> None:
    """The rows captured before capture was off stay, unlisted, until someone drops them:
    ``project list --all`` shows them and doctor names the prune that takes them all."""
    with store_session() as store:
        store.ensure_project(ProjectInfo(id="prj_one", root=tmp_path / "one"))  # 0.8.0's

    [check] = diagnostics._check_captured_projects()

    assert check.detail.startswith("1 captured directory left from before project capture")
    assert "aisquare project list --all" in check.detail
    assert check.detail.endswith("aisquare project prune --captured-only --older-than 0")


def test_doctor_names_the_rows_that_prune_keeps_and_what_drops_them(tmp_path: Path) -> None:
    """Review of #259, round 17: doctor counted every captured row and said "drop them" with
    a prune that keeps each captured row holding a context entry, whatever ``--older-than``
    says. After running it, those rows remained, and every doctor run repeated the same
    count with the same advice. The prune is named for the rows it drops, and the rows it
    keeps are named apart, with what drops them."""
    with store_session() as store:
        for name in ("scratch", "notes"):
            store.ensure_project(ProjectInfo(id=f"prj_{name}", root=tmp_path / name))
        store.add(new_entry("a fact", "project", "prj_notes", [], "cli"))

    [check] = diagnostics._check_captured_projects()

    said = " ".join(check.detail.split())
    assert said.startswith("1 captured directory left from before project capture"), said
    assert "drop them: aisquare project prune --captured-only --older-than 0" in said
    assert "1 more holds saved context, which that prune keeps: notes" in said
    assert f"aisquare project forget {tmp_path / 'notes'}" in said

    from aisquare.services import project as project_service

    plan = project_service.prune_candidates(missing=False, worktrees=False, captured_older_than=0)
    assert [c.project.id for c in plan] == ["prj_scratch"], "the prune drops what doctor says"
    with store_session() as store:
        store.forget_project("prj_scratch")  # what that prune does
    [check] = diagnostics._check_captured_projects()
    said = " ".join(check.detail.split())
    assert said.startswith(
        "1 captured directory left from before project capture was turned off holds saved "
        "context, which aisquare project prune keeps: notes"
    ), said
    assert "--older-than 0" not in said


def test_doctor_asks_only_the_captured_rows_and_no_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 18: with capture off, doctor built the whole prune plan to word
    its line: a live-agent query for every project, a ``stat`` of every root (some on network
    mounts) though no reason asked for one, and a root ``is_dir`` raised ``EACCES`` for hid
    the whole line. It asks the captured rows alone, and no root."""
    from aisquare.core.store import SqliteStore

    added = ProjectInfo(id="prj_added", root=tmp_path / "locked" / "added")
    with store_session() as store:
        store.onboard_project(added)
        store.ensure_project(ProjectInfo(id="prj_scratch", root=tmp_path / "scratch"))

    locked, real_stat = tmp_path / "locked", os.stat

    def no_search(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        # Under a directory this user may not search: ``Path.is_dir`` raised for it up to
        # Python 3.13, and from 3.14 says False (round 19).
        if isinstance(path, (str, os.PathLike)) and Path(path).is_relative_to(locked):
            raise PermissionError(13, "Permission denied", str(path))
        return real_stat(path, *args, **kwargs)

    asked: list[str] = []
    real = SqliteStore.fleet_agents

    def counted(self: SqliteStore, project_id: str, **kwargs: bool) -> list[FleetAgent]:
        asked.append(project_id)
        return real(self, project_id, **kwargs)

    monkeypatch.setattr(os, "stat", no_search)
    monkeypatch.setattr(SqliteStore, "fleet_agents", counted)

    [check] = diagnostics._check_captured_projects()

    assert check.detail.startswith("1 captured directory left from before project capture")
    assert "prj_added" not in asked, "an added project's agents were asked"
    from aisquare.services import project as project_service

    plan = project_service.prune_candidates(missing=True, worktrees=False)
    assert [c.project.id for c in plan] == ["prj_scratch"], "a root it cannot ask is not missing"


def test_doctor_asks_whether_a_captured_row_holds_context_without_loading_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 19: the prune plan loaded and built every entry of each captured
    row to learn whether one existed, on every doctor run with capture off, and the line
    opened three store sessions and read the setting twice. One row asked for, in one
    session, the setting read once."""
    import contextlib
    from collections.abc import Iterator

    from aisquare.core import store as store_core
    from aisquare.core.store import SqliteStore
    from aisquare.services import project as project_service

    with store_session() as store:
        for name in ("scratch", "notes", "gone"):
            store.ensure_project(ProjectInfo(id=f"prj_{name}", root=tmp_path / name))
        store.add(new_entry("a fact", "project", "prj_notes", [], "cli"))
        store.add(new_entry("a fact", "project", "prj_gone", [], "cli"))
        store.add(new_entry("for everyone", "user", None, [], "cli"))
        store.forget_project("prj_gone")
        assert [store.has_entries(f"prj_{n}") for n in ("scratch", "notes", "gone")] == [
            False,
            True,
            False,
        ], "as entries('project') lists them"

    loaded: list[str | None] = []
    real_entries, real_session = SqliteStore.entries, store_core.store_session
    sessions: list[int] = []
    settings: list[int] = []
    capture_on = project_service.capture_on

    def entries(self: SqliteStore, *args: Any, **kwargs: Any) -> Any:
        loaded.append(kwargs.get("project_id"))
        return real_entries(self, *args, **kwargs)

    @contextlib.contextmanager
    def counted() -> Iterator[Any]:
        sessions.append(1)
        with real_session() as store:
            yield store

    def setting() -> bool:
        settings.append(1)
        return capture_on()

    monkeypatch.setattr(SqliteStore, "entries", entries)
    monkeypatch.setattr(diagnostics, "store_session", counted)
    monkeypatch.setattr(project_service, "store_session", counted)
    monkeypatch.setattr(project_service, "capture_on", setting)

    [check] = diagnostics._check_captured_projects()

    said = " ".join(check.detail.split())
    assert said.startswith("1 captured directory left from before project capture"), said
    assert "1 more holds saved context, which that prune keeps: notes" in said
    assert loaded == [], "entries were loaded"
    assert (len(sessions), len(settings)) == (1, 1)


def _captured_rows(tmp_path: Path, prefix: str, count: int) -> list[ProjectInfo]:
    return [
        ProjectInfo(id=f"prj_{prefix}{n}", root=tmp_path / f"{prefix}{n}") for n in range(count)
    ]


@pytest.mark.parametrize(
    ("dropped", "kept"), [(2, 0), (2, 1), (1, 2), (0, 1), (0, 4)], ids=lambda n: str(n)
)
def test_doctors_capture_off_line_for_each_count(tmp_path: Path, dropped: int, kept: int) -> None:
    """Review of #259, round 18 (12/12): the line's kept part was written out twice, in
    near-identical branches. Put together once, it reads as it did, for each count of rows
    the prune drops and keeps."""
    from aisquare.models import PruneCandidate
    from aisquare.services import install_route

    gone, held = _captured_rows(tmp_path, "d", dropped), _captured_rows(tmp_path, "k", kept)
    plan = [PruneCandidate(project=p, reason="captured") for p in gone]
    forgets = [
        install_route.command_line(["aisquare", "project", "forget", str(p.root)]) for p in held
    ]
    drop = f"drop it with {forgets[0]}" if kept == 1 else f"drop them ({'; '.join(forgets)})"
    names = ", ".join(p.root.name for p in held[:3]) + (f", +{kept - 3} more" if kept > 3 else "")
    left = "left from before project capture was turned off"
    listed = "listed only by aisquare project list --all"
    wanted = {
        (2, 0): f"2 captured directories {left} (a hooked session ran there; nothing added it "
        f"on purpose) — {listed}; drop them: aisquare project prune --captured-only "
        "--older-than 0",
        (2, 1): f"; 1 more holds saved context, which that prune keeps: {names} — {drop}",
        (1, 2): f"; 2 more hold saved context, which that prune keeps: {names} — {drop}",
        (0, 1): f"1 captured directory {left} holds saved context, which "
        f"aisquare project prune keeps: {names} — {listed}; {drop}",
        (0, 4): f"4 captured directories {left} hold saved context, which "
        f"aisquare project prune keeps: {names} — {listed}; {drop}",
    }[(dropped, kept)]

    said = diagnostics._left_from_before_capture([*gone, *held], plan, {})

    assert said.endswith(wanted) if dropped and kept else said == wanted, said


def test_doctor_names_the_captured_rows_with_a_live_agent_apart_from_what_forget_drops(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Review of #259, round 18: the line put the rows kept for a live agent with those kept
    for saved context, and told the user to drop them all with ``aisquare project forget``,
    which refuses a project with live fleet agents (``project_busy``). They are named apart,
    with their agents to stop first."""
    from datetime import UTC, datetime

    from aisquare.core.ids import new_agent_id
    from aisquare.services import install_route

    rows: dict[str, ProjectInfo] = {}
    for name in ("scratch", "notes", "busy", "both"):
        (tmp_path / name).mkdir()
        rows[name] = team_project(tmp_path / name)
    with store_session() as store:
        for row in rows.values():
            store.ensure_project(row)  # 0.8.0's capture
        for name in ("notes", "both"):
            store.add(new_entry("a fact", "project", rows[name].id, [], "cli"))
        for name in ("busy", "both"):
            store.upsert_fleet_agent(
                FleetAgent(
                    id=new_agent_id(),
                    project_id=rows[name].id,
                    label="coder-1",
                    role="coder",
                    pane_id="%1",
                    cwd=tmp_path / name,
                    created_at=datetime.now(tz=UTC),
                )
            )

    [check] = diagnostics._check_captured_projects()

    def forget(name: str) -> str:
        return install_route.command_line(["aisquare", "project", "forget", str(rows[name].root)])

    said = " ".join(check.detail.split())
    assert said.startswith("1 captured directory left from before project capture"), said
    assert "; 1 more holds saved context, which that prune keeps: notes — drop it with " in said
    assert (
        "; 2 more have fleet agents still running, which that prune keeps and aisquare project "
        f"forget refuses: both, busy — stop their agents first (aisquare fleet stop --project "
        f"{rows['both'].id} <label>; aisquare fleet stop --project {rows['busy'].id} <label>), "
        f"then drop them ({forget('both')}; {forget('busy')})"
    ) in said, said
    refused = runner.invoke(app, ["--json", "project", "forget", str(tmp_path / "busy")])
    assert json.loads(refused.stdout)["error"] == "project_busy", "the premise"
    dropped = runner.invoke(app, ["project", "forget", str(tmp_path / "notes")])
    assert dropped.exit_code == 0, dropped.output


def test_doctor_names_a_captured_hub_prune_keeps_apart_with_the_project_that_reads_it(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Review of #259, round 34, 3/10: doctor asked a captured row's own live agents alone,
    but prune and forget hold a hub, a board another project's agents read, running or a
    restart away. ``tools``, captured before capture was off, is the board ``api``'s stopped
    manager recorded: doctor counted it under "drop them" with a prune that keeps it, and
    said so again after that prune. ``notes``, holding saved context and read by ``api``'s
    running coder, was told to drop with a forget that refuses it. Both are named apart,
    with the project that reads them and the forget that frees them, as ``forget`` says, and
    ``api``'s stop first, since its coder runs: ``forget`` refuses a project with live agents
    (round 35, 2/8)."""
    from datetime import UTC, datetime

    from aisquare.core.ids import new_agent_id
    from aisquare.services import install_route
    from aisquare.services import project as project_service

    rows: dict[str, ProjectInfo] = {}
    for name in ("api", "notes", "scratch", "tools"):
        (tmp_path / name).mkdir()
        rows[name] = team_project(tmp_path / name)
    with store_session() as store:
        store.onboard_project(rows["api"])  # added on purpose
        for name in ("notes", "scratch", "tools"):
            store.ensure_project(rows[name])  # 0.8.0's capture
        store.add(new_entry("a fact", "project", rows["notes"].id, [], "cli"))
        for label, board in (("manager", "tools"), ("coder-1", "notes")):
            row = store.upsert_fleet_agent(
                FleetAgent(
                    id=new_agent_id(), project_id=rows["api"].id, label=label, role="coder",
                    pane_id="%1", cwd=rows["api"].root, created_at=datetime.now(tz=UTC),
                    board=rows[board].id,
                )
            )  # fmt: skip
            if label == "manager":
                store.end_fleet_agent(row.id, exit_status=0)

    def forget(name: str) -> str:
        return install_route.command_line(["aisquare", "project", "forget", str(rows[name].root)])

    hubs = (
        "; 2 more are the boards another project's fleet agents read (api), running or stopped, "
        "which that prune keeps and aisquare project forget refuses: notes, tools — once you are "
        "done with its agents, stop the running ones "
        f"({project_service.fleet_command('stop', rows['api'])} <label>) and forget it "
        f"({forget('api')}), then drop them ({forget('notes')}; {forget('tools')})"
    )
    [check] = diagnostics._check_captured_projects()
    said = " ".join(check.detail.split())
    assert said.startswith("1 captured directory left from before project capture"), said
    assert said.endswith(hubs), said
    assert said.count("fleet stop") == 1, "neither has agents of its own to stop: api's alone"

    plan = project_service.prune_candidates(missing=False, worktrees=False, captured_older_than=0)
    report = project_service.prune(plan, purge=False)
    assert report.dropped == [rows["scratch"].id], "the prune drops what doctor says"
    for name in ("notes", "tools"):
        refused = runner.invoke(app, ["--json", "project", "forget", str(rows[name].root)])
        assert json.loads(refused.stdout)["error"] == "project_busy", name
    [check] = diagnostics._check_captured_projects()
    said = " ".join(check.detail.split())
    assert "--older-than 0" not in said, "nothing more for that prune to drop"
    assert said.startswith(
        "2 captured directories left from before project capture was turned off are the boards "
        "another project's fleet agents read (api)"
    ), said


def test_doctor_names_every_project_reading_a_captured_hub_with_what_frees_each(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Review of #259, round 35, 2/8: the hub part named one reader a row, the first, with
    its forget alone. ``tools`` is read by ``api``'s stopped manager and by ``web``'s running
    coder, ``notes`` by ``old``'s running coder, ``old`` forgotten since. The line named
    ``api`` for ``tools``, and once its forget ran, forget of ``tools`` was refused naming
    ``web``; it told no one to stop a running coder, which ``forget`` refuses; and told the
    user to forget ``old``, which matches nothing. Every reader is named, with its stop when
    its agents run and its onboard when it is forgotten, and the forgets then free both."""
    from datetime import UTC, datetime

    from aisquare.core.ids import new_agent_id
    from aisquare.services import install_route
    from aisquare.services import project as project_service

    rows: dict[str, ProjectInfo] = {}
    for name in ("api", "web", "old", "notes", "tools"):
        (tmp_path / name).mkdir()
        rows[name] = team_project(tmp_path / name)
    agents: dict[str, FleetAgent] = {}
    with store_session() as store:
        for name in ("api", "web", "old"):
            store.onboard_project(rows[name])  # added on purpose
        for name in ("notes", "tools"):
            store.ensure_project(rows[name])  # 0.8.0's capture
        for reader, label, board in (
            ("api", "manager", "tools"),
            ("web", "coder-1", "tools"),
            ("old", "coder-1", "notes"),
        ):
            agents[reader] = store.upsert_fleet_agent(
                FleetAgent(
                    id=new_agent_id(), project_id=rows[reader].id, label=label, role="coder",
                    pane_id="%1", cwd=rows[reader].root, created_at=datetime.now(tz=UTC),
                    board=rows[board].id,
                )
            )  # fmt: skip
        store.end_fleet_agent(agents["api"].id, exit_status=0)
        store.forget_project(rows["old"].id)

    def forget(name: str) -> str:
        return install_route.command_line(["aisquare", "project", "forget", str(rows[name].root)])

    def stop(name: str) -> str:
        return project_service.fleet_command("stop", rows[name]) + " <label>"

    [check] = diagnostics._check_captured_projects()
    said = " ".join(check.detail.split())
    assert said == (
        "2 captured directories left from before project capture was turned off are the boards "
        "other projects' fleet agents read (old, api, web), running or stopped, which aisquare "
        "project prune keeps and aisquare project forget refuses: notes, tools — listed only by "
        "aisquare project list --all; once you are done with their agents, add the forgotten "
        f"one back ({project_service.onboard_command(rows['old'].root)}), stop the running ones "
        f"({stop('old')}; {stop('web')}) and forget them ({forget('old')}; {forget('api')}; "
        f"{forget('web')}), then drop them ({forget('notes')}; {forget('tools')})"
    ), said

    # Followed: each step reaches its project, and the forgets of the hubs then go.
    assert runner.invoke(app, ["project", "onboard", str(rows["old"].root)]).exit_code == 0
    with store_session() as store:
        for reader in ("old", "web"):
            store.end_fleet_agent(agents[reader].id, exit_status=0)  # its `fleet stop`
    for name in ("old", "api", "web", "notes", "tools"):
        done = runner.invoke(app, ["project", "forget", str(rows[name].root)])
        assert done.exit_code == 0, (name, done.output)


def test_the_refusal_and_doctor_free_a_hubs_readers_by_the_same_steps(tmp_path: Path) -> None:
    """Review of #259, round 36, 7/9: doctor wrote out what frees a hub's readers a second
    time beside ``ProjectBusyError``, with rules of its own: doctor always said to forget a
    reader, the refusal only when one of its rows had stopped (1/9). Both are worded from
    each reader's :meth:`BoardReader.frees`: its onboard when it is forgotten, its stop when
    its agents run, then its forget, the same commands in the same order in each."""
    from datetime import UTC, datetime

    from aisquare.services import project as project_service

    hub = ProjectInfo(id="prj_hub", root=tmp_path / "hub")

    def row(project: ProjectInfo, *, ended: bool) -> FleetAgent:
        now = datetime.now(tz=UTC)
        return FleetAgent(
            id=f"a-{project.id}", project_id=project.id, label="coder-1", role="coder",
            pane_id="%1", cwd=project.root, created_at=now, board=hub.id,
            ended_at=now if ended else None,
        )  # fmt: skip

    readers = [
        project_service.BoardReader(reader, [row(reader, ended=ended)], forgotten=forgotten)
        for reader, ended, forgotten in (
            (ProjectInfo(id="prj_api", root=tmp_path / "api"), True, False),
            (ProjectInfo(id="prj_web", root=tmp_path / "web"), False, False),
            (ProjectInfo(id="prj_old", root=tmp_path / "old"), False, True),
        )
    ]
    refusal = str(project_service.ProjectBusyError(hub, readers=readers))
    _, doctor = diagnostics._what_frees([hub], {hub.id: readers})

    for reader in readers:
        said = refusal.split(f"{reader.project.root.name}'s 1 ", 1)[1].split(". It is also", 1)[0]
        frees = reader.frees()
        given = [step for step in frees if step is not None]
        assert [said.index(step) for step in given] == sorted(said.index(s) for s in given), said
        assert all(step in doctor for step in given), (given, doctor)
        for step, unsaid in (
            (frees.onboard, project_service.onboard_command(reader.project.root)),
            (frees.stop, project_service.fleet_command("stop", reader.project)),
        ):
            assert step is not None or (unsaid not in said and unsaid not in doctor), unsaid
    assert doctor.index("add the forgotten one back") < doctor.index("stop the running ones")
    assert doctor.index("stop the running ones") < doctor.index("forget them ("), doctor


def test_doctor_frees_a_row_outside_the_plan_of_a_reader_that_prune_drops(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Review of #259, round 36, 3/9: the plan's held rows were asked with the rows the prune
    drops gone, and the captured rows outside the plan without. ``scratch``, captured with no
    entries, is in the plan and dropped; its stopped manager read ``notes``, which holds
    context and is not. The line named the prune, then told the user to forget ``scratch``
    before dropping ``notes``: run after that prune, as the line lists it, the forget matched
    nothing, and ``notes`` was free already. ``notes`` is named with its own forget alone."""
    from datetime import UTC, datetime

    from aisquare.core.ids import new_agent_id
    from aisquare.services import install_route
    from aisquare.services import project as project_service

    rows: dict[str, ProjectInfo] = {}
    for name in ("notes", "scratch"):
        (tmp_path / name).mkdir()
        rows[name] = team_project(tmp_path / name)
    with store_session() as store:
        for name in ("notes", "scratch"):
            store.ensure_project(rows[name])  # 0.8.0's capture
        store.add(new_entry("a fact", "project", rows["notes"].id, [], "cli"))
        manager = store.upsert_fleet_agent(
            FleetAgent(
                id=new_agent_id(), project_id=rows["scratch"].id, label="manager", role="manager",
                pane_id="%1", cwd=rows["scratch"].root, created_at=datetime.now(tz=UTC),
                board=rows["notes"].id,
            )
        )  # fmt: skip
        store.end_fleet_agent(manager.id, exit_status=0)
    forget = install_route.command_line(["aisquare", "project", "forget", str(rows["notes"].root)])

    [check] = diagnostics._check_captured_projects()

    said = " ".join(check.detail.split())
    assert said.endswith(
        "drop them: aisquare project prune --captured-only --older-than 0; 1 more holds saved "
        f"context, which that prune keeps: notes — drop it with {forget}"
    ), said
    assert str(rows["scratch"].root) not in said, said
    plan = project_service.prune_candidates(missing=False, worktrees=False, captured_older_than=0)
    assert project_service.prune(plan, purge=False).dropped == [rows["scratch"].id]
    done = runner.invoke(app, ["project", "forget", str(rows["notes"].root)])
    assert done.exit_code == 0, done.output


def test_doctor_reads_the_rows_that_hold_a_board_once_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 35, 6/8: doctor's capture-off line read every project's fleet
    label heads (``fleet_label_heads``, a ``FleetAgent`` built a head) in the plan's holds,
    and again in ``forget_refusals``, and again since round 35's 2/8 asked the plan's held
    rows apart: doctor runs at every ``asq`` start. A plan with a dropped row, a held one,
    and a row it keeps for its context: read once, and the line as it was."""
    from datetime import UTC, datetime

    from aisquare.core.ids import new_agent_id
    from aisquare.core.store import SqliteStore

    rows: dict[str, ProjectInfo] = {}
    for name in ("api", "notes", "scratch", "tools"):
        (tmp_path / name).mkdir()
        rows[name] = team_project(tmp_path / name)
    with store_session() as store:
        store.onboard_project(rows["api"])
        for name in ("notes", "scratch", "tools"):
            store.ensure_project(rows[name])
        store.add(new_entry("a fact", "project", rows["notes"].id, [], "cli"))
        store.upsert_fleet_agent(
            FleetAgent(
                id=new_agent_id(), project_id=rows["api"].id, label="coder-1", role="coder",
                pane_id="%1", cwd=rows["api"].root, created_at=datetime.now(tz=UTC),
                board=rows["tools"].id,
            )
        )  # fmt: skip
    [before] = diagnostics._check_captured_projects()
    reads: list[int] = []
    real = SqliteStore.fleet_label_heads

    def counted(store: SqliteStore) -> list[FleetAgent]:
        reads.append(1)
        return real(store)

    monkeypatch.setattr(SqliteStore, "fleet_label_heads", counted)

    [check] = diagnostics._check_captured_projects()

    assert len(reads) == 1, reads
    assert check.detail == before.detail
    # The dropped row is counted, never named: "scratch" matched only through a temp path that
    # held it, a scratchpad's, and not under /tmp, as on CI (review of #259, round 38, 1/4).
    said = " ".join(check.detail.split())
    assert said.startswith("1 captured directory left from before"), said
    assert str(rows["scratch"].root) not in said, said
    assert "which that prune keeps: notes — " in said, said
    assert "aisquare project forget refuses: tools — " in said, said


def test_doctor_works_out_the_hold_of_each_captured_row_once_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 36, 8/9: doctor asked ``forget_refusals`` of the plan's held rows,
    whose holds the plan had worked out already (``_holds``) and thrown away: each held row's
    ``_busy`` ran again, its live agents queried and every label head scanned, on every
    ``asq`` start. The plan gives them back. ``tools`` is held, ``scratch`` dropped and
    ``notes`` kept for its context: each row's live agents are asked once, and the line is as
    it was."""
    from datetime import UTC, datetime

    from aisquare.core.ids import new_agent_id
    from aisquare.core.store import SqliteStore

    rows: dict[str, ProjectInfo] = {}
    for name in ("api", "notes", "scratch", "tools"):
        (tmp_path / name).mkdir()
        rows[name] = team_project(tmp_path / name)
    with store_session() as store:
        store.onboard_project(rows["api"])
        for name in ("notes", "scratch", "tools"):
            store.ensure_project(rows[name])
        store.add(new_entry("a fact", "project", rows["notes"].id, [], "cli"))
        store.upsert_fleet_agent(
            FleetAgent(
                id=new_agent_id(), project_id=rows["api"].id, label="coder-1", role="coder",
                pane_id="%1", cwd=rows["api"].root, created_at=datetime.now(tz=UTC),
                board=rows["tools"].id,
            )
        )  # fmt: skip
    [before] = diagnostics._check_captured_projects()
    asked: list[str] = []
    real = SqliteStore.fleet_agents

    def counted(store: SqliteStore, project_id: str, **kwargs: Any) -> list[FleetAgent]:
        if kwargs.get("live_only"):
            asked.append(project_id)
        return real(store, project_id, **kwargs)

    monkeypatch.setattr(SqliteStore, "fleet_agents", counted)

    [check] = diagnostics._check_captured_projects()

    names = {row.id: name for name, row in rows.items()}
    assert sorted(names[project_id] for project_id in asked) == ["notes", "scratch", "tools"]
    assert check.detail == before.detail
    assert "aisquare project forget" in check.detail and "tools" in check.detail


def test_doctors_stop_for_a_captured_rows_agent_names_that_project(
    work_dir: Path, tmp_path: Path
) -> None:
    """Review of #259, round 19: the line told the user to run ``aisquare fleet stop <label>``,
    which acts on the active project, never on the captured directory the line names. Run
    from the main project, with its own ``coder-1``, it stopped that one, and the captured
    row's agent ran on, so ``project forget`` still refused. The stop names the project."""
    from datetime import UTC, datetime

    from aisquare.core.ids import new_agent_id
    from aisquare.services import fleet as fleet_service
    from aisquare.services import install_route

    (tmp_path / "busy").mkdir()
    busy, main = team_project(tmp_path / "busy"), team_project(work_dir)
    with store_session() as store:
        store.ensure_project(busy)  # 0.8.0's capture
        store.onboard_project(main)
        for row in (busy, main):
            store.upsert_fleet_agent(
                FleetAgent(
                    id=new_agent_id(), project_id=row.id, label="coder-1", role="coder",
                    pane_id="%1", cwd=row.root, created_at=datetime.now(tz=UTC),
                )
            )  # fmt: skip

    [check] = diagnostics._check_captured_projects()

    said = " ".join(check.detail.split())
    stop = said.split("stop its agents first (", 1)[1].split(" <label>)", 1)[0]
    argv = install_route.split_line(stop)
    assert argv[:4] == ["aisquare", "fleet", "stop", "--project"], said
    assert fleet_service.resolve_project(argv[4]).id == busy.id, "the captured row's"
    assert fleet_service.resolve_project(None).id == main.id, "the premise: not the active one"


def test_doctors_stop_names_each_busy_captured_row_by_a_ref_of_its_own(tmp_path: Path) -> None:
    """Review of #259, round 20: with several captured rows holding live agents, the line said
    ``aisquare fleet stop --project <name> <label>, for each`` beside the rows' directory names,
    three at most. ``/a/scratch`` and ``/b/scratch`` both read "scratch", which ``--project``
    refuses as matching several projects, and a fourth row was only "+1 more". Each row gets
    its own stop, every row, naming it as the single row's does."""
    from datetime import UTC, datetime

    from aisquare.core.ids import new_agent_id
    from aisquare.services import fleet as fleet_service
    from aisquare.services import install_route

    rows: list[ProjectInfo] = []
    for parent, name in (("a", "scratch"), ("b", "scratch"), ("c", "tools"), ("d", "web")):
        (tmp_path / parent / name).mkdir(parents=True)
        rows.append(team_project(tmp_path / parent / name))
    with store_session() as store:
        for row in rows:
            store.ensure_project(row)  # 0.8.0's capture
            store.upsert_fleet_agent(
                FleetAgent(
                    id=new_agent_id(), project_id=row.id, label="coder-1", role="coder",
                    pane_id="%1", cwd=row.root, created_at=datetime.now(tz=UTC),
                )
            )  # fmt: skip

    [check] = diagnostics._check_captured_projects()

    said = " ".join(check.detail.split())
    stops = said.split("stop their agents first (", 1)[1].split("), then", 1)[0].split("; ")
    named: list[str] = []
    for stop in stops:
        argv = install_route.split_line(stop.removesuffix(" <label>"))
        assert argv[:4] == ["aisquare", "fleet", "stop", "--project"], said
        named.append(fleet_service.resolve_project(argv[4]).id)
    assert sorted(named) == sorted(row.id for row in rows), said


def test_doctors_forget_names_each_kept_captured_row_by_its_own_directory(
    tmp_path: Path,
) -> None:
    """Review of #259, round 22, 4/9: round 20 gave each busy row its own ``fleet stop``, and
    left the forget half saying ``aisquare project forget <directory>, for each`` beside the
    rows' directory names, three at most. ``/a/scratch`` and ``/b/scratch`` both read
    "scratch", and a fourth row was only "+1 more": no directory to pass for any of them.
    Each kept row gets its own forget, every row, for saved context and live agents alike."""
    from datetime import UTC, datetime

    from aisquare.core.ids import new_agent_id
    from aisquare.services import install_route

    held: list[ProjectInfo] = []
    busy: list[ProjectInfo] = []
    for parent, name, rows in (
        ("a", "scratch", held), ("b", "scratch", held), ("c", "tools", held), ("d", "web", held),
        ("e", "scratch", busy), ("f", "scratch", busy),
    ):  # fmt: skip
        (tmp_path / parent / name).mkdir(parents=True)
        rows.append(team_project(tmp_path / parent / name))
    with store_session() as store:
        for row in (*held, *busy):
            store.ensure_project(row)  # 0.8.0's capture
            store.add(new_entry("a fact", "project", row.id, [], "cli"))
        for row in busy:
            store.upsert_fleet_agent(
                FleetAgent(
                    id=new_agent_id(), project_id=row.id, label="coder-1", role="coder",
                    pane_id="%1", cwd=row.root, created_at=datetime.now(tz=UTC),
                )
            )  # fmt: skip

    [check] = diagnostics._check_captured_projects()

    said = " ".join(check.detail.split())
    assert "for each" not in said, said
    for rows, after in ((held, "hold saved context"), (busy, "have fleet agents")):
        forgets = said.split(after, 1)[1].split("drop them (", 1)[1].split(")", 1)[0]
        argvs = [install_route.split_line(each) for each in forgets.split("; ")]
        assert all(argv[:3] == ["aisquare", "project", "forget"] for argv in argvs), said
        assert sorted(Path(argv[3]) for argv in argvs) == sorted(row.root for row in rows), said


def test_one_helper_names_a_project_in_a_fleet_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 20: the project a fleet command names (its codename, else its id,
    never its nickname) and the ``fleet stop --project`` line it goes in were built twice, in
    ``ProjectBusyError`` and in doctor's capture-off line, and the next change to the rule had
    to be made in both. Both take it from ``project_service.fleet_command``."""
    from aisquare.services import install_route
    from aisquare.services import project as project_service

    named = ProjectInfo(id="prj_named", root=tmp_path / "web", codename="calm-otter", nickname="w")
    plain = ProjectInfo(id="prj_plain", root=tmp_path / "web")
    assert project_service.fleet_command("stop", named) == (
        "aisquare fleet stop --project calm-otter"
    )
    assert project_service.fleet_command("reap", plain) == "aisquare fleet reap --project prj_plain"
    # Words after the project (a label, a flag) are quoted for this shell too (round 21).
    restart = project_service.fleet_command("restart", named, "coder 1", "--fresh")
    assert install_route.split_line(restart) == [
        *("aisquare", "fleet", "restart", "--project", "calm-otter", "coder 1", "--fresh")
    ]

    asked: list[tuple[str, str]] = []

    def fleet_command(verb: str, project: ProjectInfo) -> str:
        asked.append((verb, project.id))
        return f"<{verb} {project.id}>"

    monkeypatch.setattr(project_service, "fleet_command", fleet_command)
    busy = str(project_service.ProjectBusyError(named, []))
    said = diagnostics._left_from_before_capture([named], [], {named.id: []})

    assert "(<stop prj_named> <label>)" in busy and "<reap prj_named>" in busy, busy
    assert "<shutdown prj_named>" in busy, busy
    assert "stop its agents first (<stop prj_named> <label>)" in said, said
    assert ("stop", named.id) in asked and len(asked) == 4


# --- the setting, read --------------------------------------------------------------------


def test_the_setting_is_parsed_once_until_config_toml_changes(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 17: ``capture_on()`` read, parsed and validated config.toml on
    every call — twice a SessionStart, once a prompt, and on asq's UI thread every refresh
    tick. It is parsed again only when the file changes (``load_config``'s memo, since
    round 18: tests/test_config.py)."""
    from aisquare.core import config as config_core
    from aisquare.core import paths
    from aisquare.services import project as project_service

    real = config_core._parse_toml
    loads: list[bytes] = []

    def counted(raw: bytes) -> dict[str, object]:
        loads.append(raw)
        return real(raw)

    monkeypatch.setattr(config_core, "_parse_toml", counted)
    config = paths.config_path()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("[projects]\ncapture = false\n", encoding="utf-8")

    assert [project_service.capture_on() for _ in range(3)] == [False, False, False]
    assert len(loads) == 1, "parsed once"
    config.write_text("[projects]\ncapture = true\n", encoding="utf-8")
    assert project_service.capture_on() is True
    assert len(loads) == 2, "and again once it changed"


def test_a_read_of_the_setting_that_failed_is_asked_again(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 18: the fallback a failed read gave (capture off) was kept under
    the file's unchanged ``stat``, so one read a retry would clear — the file held by a
    backup tool, an NFS ``EIO`` — turned capture off until config.toml was next edited."""
    from aisquare.core import config as config_core
    from aisquare.core import paths
    from aisquare.services import project as project_service

    real = config_core.load_config
    failures = [OSError(5, "Input/output error")]

    def flaky(path: Path | None = None, **kwargs: bool) -> config_core.AppConfig:
        if failures:
            raise failures.pop()
        return real(path, **kwargs)

    monkeypatch.setattr(project_service, "load_config", flaky)
    config = paths.config_path()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("[projects]\ncapture = true\n", encoding="utf-8")

    assert project_service.capture_on() is False, "a failed read costs the setting, once"
    assert project_service.capture_on() is True, "and is read again on the next ask"


def test_asking_whether_a_project_is_tracked_never_writes(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 17: ``tracked()`` was a predicate that wrote — with capture on it
    captured the directory first, which is why a session start had to pass ``capture=False``
    to ask it. Asking writes nothing now; ``capture()`` is the write, and only with capture
    on."""
    from aisquare.services import project as project_service

    project = team_project(work_dir)
    with store_session() as store:
        assert project_service.tracked(store, project) is False
        project_service.capture(store, project)  # off: nothing
        monkeypatch.setattr(project_service, "capture_on", lambda: True)
        assert project_service.tracked(store, project) is True
        assert _rows() == [], "asking wrote nothing"
        project_service.capture(store, project)
        assert [p.id for p in store.captured_projects()] == [project.id]
