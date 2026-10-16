"""``[projects] capture`` off, the default since 0.8.1: no project row unless one is added on
purpose (#139) — the board, a destination and doctor.

The rest of the call sites are tested beside their own: the hooks in tests/test_hooks.py
and tests/test_ci_augment.py, the shell in tests/test_ui_shell.py, the fleet in
tests/test_fleet_service.py, the MCP server in tests/test_serve.py.
"""

from __future__ import annotations

import json
from pathlib import Path

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
    assert "1 more holds saved context or a live agent, which that prune keeps: notes" in said
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
        "context or a live agent, which aisquare project prune keeps: notes"
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

    locked, is_dir = tmp_path / "locked", Path.is_dir

    def no_stat(self: Path) -> bool:
        if self.is_relative_to(locked):  # under a directory this user may not search
            raise PermissionError(13, "Permission denied", str(self))
        return is_dir(self)

    asked: list[str] = []
    real = SqliteStore.fleet_agents

    def counted(self: SqliteStore, project_id: str, **kwargs: bool) -> list[FleetAgent]:
        asked.append(project_id)
        return real(self, project_id, **kwargs)

    monkeypatch.setattr(Path, "is_dir", no_stat)
    monkeypatch.setattr(SqliteStore, "fleet_agents", counted)

    [check] = diagnostics._check_captured_projects()

    assert check.detail.startswith("1 captured directory left from before project capture")
    assert "prj_added" not in asked, "an added project's agents were asked"
    from aisquare.services import project as project_service

    plan = project_service.prune_candidates(missing=True, worktrees=False)
    assert [c.project.id for c in plan] == ["prj_scratch"], "a root it cannot ask is not missing"


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
    forget = (
        install_route.command_line(["aisquare", "project", "forget", str(held[0].root)])
        if kept == 1
        else "aisquare project forget <directory>, for each"
    )
    names = ", ".join(p.root.name for p in held[:3]) + (f", +{kept - 3} more" if kept > 3 else "")
    left = "left from before project capture was turned off"
    listed = "listed only by aisquare project list --all"
    wanted = {
        (2, 0): f"2 captured directories {left} (a hooked session ran there; nothing added it "
        f"on purpose) — {listed}; drop them: aisquare project prune --captured-only "
        "--older-than 0",
        (2, 1): f"; 1 more holds saved context or a live agent, which that prune keeps: {names} "
        f"— drop it with {forget}",
        (1, 2): f"; 2 more hold saved context or a live agent, which that prune keeps: {names} "
        f"— drop them with {forget}",
        (0, 1): f"1 captured directory {left} holds saved context or a live agent, which "
        f"aisquare project prune keeps: {names} — {listed}; drop it with {forget}",
        (0, 4): f"4 captured directories {left} hold saved context or a live agent, which "
        f"aisquare project prune keeps: {names} — {listed}; drop them with {forget}",
    }[(dropped, kept)]

    said = diagnostics._left_from_before_capture([*gone, *held], plan)

    assert said.endswith(wanted) if dropped and kept else said == wanted, said


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

    def flaky(path: Path | None = None) -> config_core.AppConfig:
        if failures:
            raise failures.pop()
        return real(path)

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
