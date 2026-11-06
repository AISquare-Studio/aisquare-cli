"""A Claude Code ``fleet spawn --worktree`` runs the repository's hooks, as 0.9.2's plain
``git -C`` did, in a project where no agent that sandboxes its tools has run (review r40 of
#113, finding 6); where one has, none run, and the spawn says why (the owner's ruling, #97).

Every other git aisquare runs points ``core.hooksPath`` at nothing (``core.workspace.GIT_OWN``):
any hook may be one a sandboxed agent changed, and aisquare runs git outside its sandbox (review
r11 of #113, finding 4). #96 guarded only a hooks folder inside the tree; its verification found
``.git/hooks`` entries that lead into the tree (a linked script, a linked folder) and stubs that
run a config in it (husky v4, lefthook, pre-commit), which no path check catches (seq 11250, F1).
So where such an agent spawns or has run (a session, or a fleet row with none yet), no hook of
the repository's runs, and the spawn says one line naming why, unless the repository has none.
Both questions fail closed: a store that cannot say is a yes, a git that cannot say has hooks.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import claude_accounts as core
from aisquare.core import sandbox_roots, workspace
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, ProjectInfo, TeamSession
from aisquare.services import fleet as fleet_service
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
from tests.test_codex_accounts import _sign_in
from tests.test_fleet_service import (  # noqa: F401 - fixtures, by name
    FakeTmux,
    claude_on_path,
    clock,
    project,
    repo,
    tmux,
)

posix = pytest.mark.skipif(
    os.name == "nt",
    # Git for Windows' sh reads the marker's backslashed path as escapes, as
    # test_fleet_service's own hook test skips it.
    reason="a POSIX hook script",
)

KINDS = ["git-hooks", "linked-script", "linked-folder", "husky"]
"""The repository's ``post-checkout``: git's own ``.git/hooks``; a link from there to a script in
the tree, and the whole folder linked into the tree (seq 11250's F1); husky's in-tree
``core.hooksPath``."""

SKIPPED = "made its worktree without the repository's hooks: "


def _post_checkout(work: ProjectInfo, tmp_path: Path, kind: str) -> Path:
    """A ``post-checkout`` of ``kind`` (:data:`KINDS`) that leaves a mark when it runs."""
    root = work.root
    marker = tmp_path / "post-checkout-ran"
    if kind == "git-hooks":
        script = root / ".git" / "hooks" / "post-checkout"
    elif kind == "linked-script":
        script = root / "scripts" / "post-checkout"
        (root / ".git" / "hooks").mkdir(exist_ok=True)
        (root / ".git" / "hooks" / "post-checkout").symlink_to("../../scripts/post-checkout")
    elif kind == "linked-folder":
        script = root / ".githooks" / "post-checkout"
        hooks = root / ".git" / "hooks"
        if hooks.exists():
            for sample in hooks.iterdir():
                sample.unlink()
            hooks.rmdir()
        hooks.symlink_to("../.githooks")
    else:
        script = root / ".husky" / "post-checkout"
        subprocess.run(["git", "-C", str(root), "config", "core.hooksPath", ".husky"], check=True)
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    script.chmod(0o755)
    return marker


def _codex_ran_here(work: ProjectInfo, *, session: bool = True, ended: bool = False) -> None:
    """A Codex agent that ran in ``work``: its session, or (``session=False``) only its fleet
    row, spawned and never registered; ``ended``, the agent has finished (#99: the case that
    happens, a row ends as soon as its agent exits)."""
    now = datetime.now(tz=UTC)
    over = now if ended else None
    with store_session() as store:
        if session:
            store.upsert_session(
                TeamSession(
                    id="codex-ran-here",
                    project_id=work.id,
                    role="coder",
                    started_at=now,
                    last_seen_at=now,
                    ended_at=over,
                    agent="codex",
                )
            )
        else:
            store.upsert_fleet_agent(
                FleetAgent(
                    id="agt_codex_ran_here",
                    project_id=work.id,
                    label="coder-codex",
                    role="coder",
                    agent="codex",
                    binary="codex",
                    pane_id="%77",
                    cwd=work.root,
                    created_at=now,
                    ended_at=over,
                )
            )


def _said(receipt: fleet_service.SpawnReceipt) -> list[str]:
    return [note for note in receipt.notes if note.startswith(SKIPPED)]


@posix
@pytest.mark.parametrize("kind", KINDS)
def test_a_claude_only_project_runs_every_hook_as_0_9_2_did(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
    kind: str,
) -> None:
    marker = _post_checkout(project, tmp_path, kind)

    receipt = fleet_service.spawn(project, "coder", worktree=True)

    assert receipt.agent.worktree is not None and receipt.agent.cwd != project.root
    assert marker.exists(), "0.9.2 ran the repository's post-checkout for the worktree"
    assert _said(receipt) == []


@posix
@pytest.mark.parametrize("session", [True, False], ids=["its-session", "its-row-alone"])
@pytest.mark.parametrize("kind", KINDS)
def test_where_a_codex_agent_ran_no_hook_runs_and_the_spawn_says_why(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
    kind: str,
    session: bool,
) -> None:
    """Any hook may be one the Codex agent changed: a link from ``.git/hooks`` into the tree
    too (seq 11250's F1, which #96 ran). Registered or only spawned (#96's pin gap 6)."""
    marker = _post_checkout(project, tmp_path, kind)
    _codex_ran_here(project, session=session)

    receipt = fleet_service.spawn(project, "coder", worktree=True)

    assert receipt.agent.worktree is not None
    assert not marker.exists(), "the hook may be one the Codex agent changed"
    [said] = _said(receipt)
    assert said == (
        SKIPPED + "a Codex agent has run in this project, and a hook such an agent could have "
        "changed would run outside its sandbox (a project where none has run runs them)"
    )


@posix
@pytest.mark.parametrize("kind", ["git-hooks", "linked-script"])
def test_a_codex_spawn_runs_no_hook_and_says_why(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
    kind: str,
) -> None:
    marker = _post_checkout(project, tmp_path, kind)
    slot = core.create_account("codex")
    _sign_in(slot.config_dir, "x@example.com")

    receipt = fleet_service.spawn(
        project, "coder", agent="codex", worktree=True, account=str(slot.slot)
    )

    assert receipt.agent.worktree is not None
    assert not marker.exists(), "a sandboxed agent may have changed the hook"
    [said] = _said(receipt)
    assert said.startswith(SKIPPED + "Codex sandboxes its tools, "), said


def test_a_repository_with_no_hooks_is_told_nothing(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """Guarded all the same, but with no hook to skip there is nothing to say: git's own
    ``*.sample`` files are no hooks."""
    _codex_ran_here(project)
    assert not workspace.repository_has_hooks(project.root)

    receipt = fleet_service.spawn(project, "coder", worktree=True)

    assert receipt.agent.worktree is not None and _said(receipt) == []


# --- failing closed (#97, seq 11250's F2) ------------------------------------------------------


def test_a_store_that_cannot_say_whether_a_sandboxed_agent_ran_is_a_yes(
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreadable() -> Iterator[None]:
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(fleet_service, "store_session", unreadable)

    said = fleet_service._repository_hooks_run(project, get_adapter("claude-code"))

    assert said is not None and "the store could not say whether" in said


def test_a_git_that_cannot_say_has_hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tree = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    assert not workspace.repository_has_hooks(tree), "control: git's samples alone"

    (tree / ".git" / "config").write_text("[core\n\tbroken = \n", encoding="utf-8")
    assert workspace.repository_has_hooks(tree), "a config git cannot read"

    def no_git(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("git is gone")

    monkeypatch.setattr("aisquare.core.workspace.subprocess.run", no_git)
    assert workspace.repository_has_hooks(tree), "no git to ask"


@posix
def test_repository_has_hooks_reads_every_place_a_hook_can_be(tmp_path: Path) -> None:
    tree = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    hooks = tree / ".git" / "hooks"
    assert not workspace.repository_has_hooks(tree), "git's own *.sample files only"
    (hooks / "post-checkout").write_text("#!/bin/sh\n", encoding="utf-8")
    assert workspace.repository_has_hooks(tree)
    (hooks / "post-checkout").unlink()
    for sample in hooks.iterdir():
        sample.unlink()
    hooks.rmdir()
    (tree / ".githooks").mkdir()
    (tree / ".githooks" / "post-merge").write_text("#!/bin/sh\n", encoding="utf-8")
    hooks.symlink_to("../.githooks")
    assert workspace.repository_has_hooks(tree), "a linked folder's"
    hooks.unlink()
    assert not workspace.repository_has_hooks(tree), "no folder at all"
    subprocess.run(["git", "-C", str(tree), "config", "core.hooksPath", ".husky/_"], check=True)
    assert workspace.repository_has_hooks(tree), "a hooks folder of the repository's"


# --- an agent that finished, and the durable record (#99, seq 11262) --------------------------


@posix
@pytest.mark.parametrize("session", [True, False], ids=["its-ended-session", "its-ended-row"])
def test_a_codex_agent_that_has_finished_still_guards(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
    session: bool,
) -> None:
    """The case that happens: the Codex agent exited (its row ends at once) or its session
    ended. Counting only live rows or open sessions (11262's mutants M3, M4) ran the hook it
    may have changed."""
    marker = _post_checkout(project, tmp_path, "linked-script")
    _codex_ran_here(project, session=session, ended=True)

    receipt = fleet_service.spawn(project, "coder", worktree=True)

    assert not marker.exists()
    assert len(_said(receipt)) == 1


def _codex_session_start(tmp_path: Path, cwd: Path) -> None:
    """Codex's SessionStart, through the hook command `agents connect codex` installs, in
    ``cwd``: a Codex started there, by aisquare or by hand."""
    from typer.testing import CliRunner

    from aisquare.cli.app import app

    payload = {"session_id": "codex-here", "cwd": str(cwd), "hook_event_name": "SessionStart"}
    home = tmp_path / "codex-home"
    result = CliRunner().invoke(
        app, ["hook", "codex", "--config-dir", str(home)], input=json.dumps(payload)
    )
    assert result.exit_code == 0, result.output


@posix
def test_project_fresh_does_not_forget_where_a_codex_agent_could_write(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
) -> None:
    """11262's F2 (a): `project fresh` deletes the project's sessions and rows, and the checkout
    keeps the hooks the agent edited. The record of where it could write is kept."""
    from typer.testing import CliRunner

    from aisquare.cli.app import app

    marker = _post_checkout(project, tmp_path, "linked-script")
    _codex_session_start(tmp_path, project.root)
    fresh = CliRunner().invoke(app, ["project", "fresh", "-P", project.id, "--yes"])
    assert fresh.exit_code == 0, fresh.output
    with store_session() as store:
        left = [session.agent for session in store.team_sessions(project.id)]
        left += [row.agent for row in store.fleet_agents(project.id)]
    assert "codex" not in left, "fresh forgot the Codex agent's session"

    receipt = fleet_service.spawn(project, "coder", worktree=True)

    assert not marker.exists(), "the hook the Codex agent may have changed did not run"
    [said] = _said(receipt)
    assert f"a Codex agent ran in {os.path.realpath(project.root)} (" in said, said


@posix
@pytest.mark.parametrize("where", ["parent", "inside"])
def test_a_codex_session_in_a_parent_folder_guards_every_repository_inside_it(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
    where: str,
) -> None:
    """11262's F2 (b): a Codex started in a folder of several repositories (not one itself)
    may write each of them; the repository, a project of its own, is guarded. So is one a
    Codex ran inside (a folder of it, or a worktree of its)."""
    parent = project.root.parent
    assert not (parent / ".git").exists()
    marker = _post_checkout(project, tmp_path, "husky")
    inside = project.root / "packages" / "app"
    inside.mkdir(parents=True)
    _codex_session_start(tmp_path, parent if where == "parent" else inside)

    receipt = fleet_service.spawn(project, "coder", worktree=True)

    assert not marker.exists()
    assert len(_said(receipt)) == 1


@posix
def test_a_codex_launch_records_every_root_its_sandbox_may_write(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`aisquare launch` of Codex, in a folder of its own with a directory added: both are
    recorded (``AgentAdapter.sandbox_writes``), the temporary directories are not, and the
    repository it may write through ``--add-dir`` is guarded."""
    from aisquare.core import sandbox_roots
    from aisquare.services import agent_launch

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    selected = agent_launch.resolve("coder", agent="codex")
    marker = _post_checkout(project, tmp_path, "git-hooks")

    agent_launch.prepare_sandbox(selected, ["codex", "--add-dir", str(project.root)])

    with store_session() as store:
        roots = set(sandbox_roots.recorded(store))
    assert {Path(os.path.realpath(elsewhere)), Path(os.path.realpath(project.root))} <= roots
    assert Path(os.path.realpath("/tmp")) not in roots
    receipt = fleet_service.spawn(project, "coder", worktree=True)
    assert not marker.exists() and len(_said(receipt)) == 1


@posix
def test_a_claude_only_machine_records_nothing_and_runs_every_hook(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Claude Code's SessionStart records no root: its spawns run the hooks, as 0.9.2."""
    from typer.testing import CliRunner

    from aisquare.cli.app import app
    from aisquare.core import sandbox_roots

    marker = _post_checkout(project, tmp_path, "linked-script")
    payload = {"session_id": "claude-here", "cwd": str(project.root.parent)}
    started = CliRunner().invoke(app, ["hook", "session-start"], input=json.dumps(payload))
    assert started.exit_code == 0, started.output

    receipt = fleet_service.spawn(project, "coder", worktree=True)

    with store_session() as store:
        assert sandbox_roots.recorded(store) == {}
    assert marker.exists() and _said(receipt) == []


def test_a_hooks_folder_git_cannot_name_has_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """11262's M1: `rev-parse --git-path hooks` failing, with no core.hooksPath set, is a
    repository that may have hooks: the line is said."""
    real = subprocess.run

    def answering(argv: list[str], *args: object, **kwargs: object) -> object:
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(argv, 128, stdout="", stderr="fatal: no")
        if "config" in argv:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        return real(argv, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr("aisquare.core.workspace.subprocess.run", answering)
    assert workspace.repository_has_hooks(tmp_path)


def test_recording_where_it_writes_never_stops_a_launch(
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An adapter that cannot say what its sandbox writes still records the folder it starts
    in; a store that cannot be written costs the record, never the launch."""
    from aisquare.core import sandbox_roots
    from aisquare.services import agent_launch

    here = tmp_path / "here"
    here.mkdir()
    monkeypatch.chdir(here)
    selected = agent_launch.resolve("coder", agent="codex")

    def cannot_say(*args: object, **kwargs: object) -> None:
        raise RuntimeError("its config would not parse")

    monkeypatch.setattr(type(selected.adapter), "sandbox_writes", cannot_say)
    agent_launch.record_sandbox_roots(selected, ["codex"])
    with store_session() as store:
        assert Path(os.path.realpath(here)) in sandbox_roots.recorded(store)

    def unwritable() -> Iterator[None]:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(agent_launch, "store_session", unwritable)
    agent_launch.record_sandbox_roots(selected, ["codex"])  # says nothing, raises nothing


# --- the record's line, and a way out short of a purge (#101, seq 11281) ------------------------


@posix
def test_project_forget_does_not_forget_where_a_codex_agent_could_write(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
) -> None:
    """11281's MX3: `project forget --purge` deletes the project's sessions and rows through
    another path than fresh; the record of where the agent could write is kept."""
    from typer.testing import CliRunner

    from aisquare.cli.app import app
    from aisquare.core.orchestrator import team_project

    marker = _post_checkout(project, tmp_path, "linked-script")
    _codex_session_start(tmp_path, project.root)
    forgot = CliRunner().invoke(app, ["project", "forget", str(project.root), "--purge"])
    assert forgot.exit_code == 0, forgot.output
    again = team_project(project.root)
    with store_session() as store:
        store.onboard_project(again)
        assert not store.team_sessions(again.id), "forget took the Codex session"

    receipt = fleet_service.spawn(again, "coder", worktree=True)

    assert not marker.exists()
    assert len(_said(receipt)) == 1


@posix
def test_a_root_of_an_agent_this_release_cannot_read_still_guards(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
) -> None:
    """11281's MX2: only an agent that sandboxes its tools records a root, so one this release
    does not know (a later release's, after a rollback) still guards."""
    marker = _post_checkout(project, tmp_path, "husky")
    with store_session() as store:
        store.set_meta(
            f"sandbox-root:{os.path.realpath(project.root.parent)}",
            json.dumps({"agent": "gemini-cli", "at": "2026-11-05T10:00:00+00:00"}),
        )

    receipt = fleet_service.spawn(project, "coder", worktree=True)

    assert not marker.exists()
    [said] = _said(receipt)
    assert "an agent that sandboxes its tools ran in" in said, said
    assert "--forget-root" in said


@posix
def test_the_line_names_the_folder_the_agent_ran_in_and_how_to_forget_it(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
) -> None:
    """11281's F2: a session in a parent folder guards every repository under it; the line
    names that folder, the agent and the day, never "this project", and the command that
    forgets it."""
    import shlex

    parent = Path(os.path.realpath(project.root.parent))
    _post_checkout(project, tmp_path, "husky")
    _codex_session_start(tmp_path, parent)

    [said] = _said(fleet_service.spawn(project, "coder", worktree=True))

    assert "this project" not in said
    assert f"a Codex agent ran in {parent} (2" in said, said
    forget = shlex.join(["aisquare", "agents", "disconnect", "codex", "--forget-root", str(parent)])
    assert said.endswith(f"`{forget}` forgets it"), said


@posix
def test_agents_status_lists_the_roots_and_disconnect_forgets_one(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The way out short of `uninstall --purge`: list with `agents status --roots`, forget one
    with `agents disconnect <agent> --forget-root <path>`, asked first or --yes. Forgotten,
    a worktree there runs its hooks again (no session of the agent's is in the project)."""
    from typer.testing import CliRunner

    from aisquare.cli.app import app

    runner = CliRunner()
    parent = Path(os.path.realpath(project.root.parent))
    marker = _post_checkout(project, tmp_path, "husky")
    _codex_session_start(tmp_path, parent)

    listed = runner.invoke(app, ["agents", "status", "--roots"])
    as_json = json.loads(
        runner.invoke(app, ["--json", "agents", "status", "codex", "--roots"]).stdout
    )
    unasked = runner.invoke(app, ["agents", "disconnect", "codex", "--forget-root", str(parent)])
    with store_session() as store:
        assert parent in sandbox_roots.recorded(store), "refused off a terminal without --yes"
    forgot = runner.invoke(
        app, ["agents", "disconnect", "codex", "--forget-root", str(parent), "--yes"]
    )
    missing = runner.invoke(
        app, ["agents", "disconnect", "codex", "--forget-root", str(parent), "--yes"]
    )

    assert listed.exit_code == 0 and str(parent) in listed.stdout and "codex" in listed.stdout
    assert [entry["path"] for entry in as_json] == [str(parent)]
    assert as_json[0]["agent"] == "codex" and as_json[0]["recorded_at"]
    assert unasked.exit_code != 0 and "needs --yes" in unasked.output
    assert forgot.exit_code == 0 and f"forgot {parent}" in forgot.stdout
    assert missing.exit_code != 0 and "no folder is recorded" in missing.output
    with store_session() as store:
        assert sandbox_roots.recorded(store) == {}
        for session in store.team_sessions(project.id):
            assert session.agent != "codex"
    receipt = fleet_service.spawn(project, "coder", worktree=True)
    assert marker.exists(), "forgotten, and no Codex session here: the hooks run"
    assert _said(receipt) == []


def test_the_home_folder_is_spelled_as_a_person_writes_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = Path(os.path.realpath(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: home)
    assert fleet_service._home_spelled(home) == "~"
    assert fleet_service._home_spelled(home / "work" / "repo") == "~/work/repo"
    elsewhere = Path("/opt/elsewhere")
    assert fleet_service._home_spelled(elsewhere) == str(elsewhere), "as this OS writes it"


# --- #101's reopen 1 (seq 11302): the list and the forget, by name; the four claims pinned ------


def _root(path: Path, agent: str = "codex", at: str = "2026-11-05T10:00:00+00:00") -> Path:
    """``path`` recorded as a root of ``agent`` (a value that cannot be read for ``agent=""``,
    as a hand-edited or a later release's record), as `core.sandbox_roots.record` keys it."""
    real = Path(os.path.realpath(path))
    real.mkdir(parents=True, exist_ok=True)
    value = json.dumps({"agent": agent, "at": at}) if agent else "not json"
    with store_session() as store:
        store.set_meta(f"{sandbox_roots.PREFIX}{real}", value)
    return real


def _cli(*argv: str) -> Any:
    from typer.testing import CliRunner

    from aisquare.cli.app import app

    return CliRunner().invoke(app, list(argv))


def test_status_roots_of_an_agent_with_none_says_so_of_that_agent_alone(
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Reopen 1, item 1: four Codex roots guarded, and `agents status claude-code --roots`
    said "no folder is recorded: every fleet worktree runs its repository's hooks"."""
    for name in ("a", "b"):
        _root(tmp_path / name)

    none = _cli("agents", "status", "claude-code", "--roots")
    unknown = _cli("agents", "status", "nosuch", "--roots")
    without = _cli("agents", "status", "nosuch")

    assert none.exit_code == 0, none.output
    assert " ".join(none.stdout.split()) == (
        "no folder is recorded for Claude Code (2 are, of other agents: "
        "aisquare agents status --roots)"
    )
    assert (
        (unknown.exit_code, unknown.output)
        == (without.exit_code, without.output)
        == (
            1,
            "✗ unknown agent: nosuch\n",
        )
    )


def test_forgetting_a_root_under_another_agents_name_is_refused_naming_the_command(
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Reopen 1, item 2: `disconnect claude-code --forget-root` forgot Codex's root, and so did
    `disconnect nosuch`."""
    import shlex

    root = _root(tmp_path / "work")

    other = _cli("agents", "disconnect", "claude-code", "--forget-root", str(root), "--yes")
    unknown = _cli("--json", "agents", "disconnect", "nosuch", "--forget-root", str(root), "--yes")

    assert other.exit_code == 1
    forget = shlex.join(["aisquare", "agents", "disconnect", "codex", "--forget-root", str(root)])
    assert " ".join(other.output.split()) == (
        f"✗ {root} was recorded by a Codex agent, not claude-code: forget it with {forget}"
    )
    assert unknown.exit_code == 1 and json.loads(unknown.stdout)["error"] == "unknown_agent"
    with store_session() as store:
        assert root in sandbox_roots.recorded(store), "neither forgot it"


@pytest.mark.parametrize("agent", ["gemini-cli", ""], ids=["unknown-agent", "unreadable"])
def test_the_command_printed_for_a_root_no_agent_of_this_release_recorded_works_pasted(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
    agent: str,
) -> None:
    """Reopen 1, item 2: such a root's line said `disconnect unknown`, a placeholder. The line
    and the list print one command for it, naming this release's agent that sandboxes its
    tools, and that command, pasted, forgets it."""
    import shlex

    _post_checkout(project, tmp_path, "husky")
    root = _root(project.root.parent, agent=agent)
    forget = shlex.join(["aisquare", "agents", "disconnect", "codex", "--forget-root", str(root)])

    [said] = _said(fleet_service.spawn(project, "coder", worktree=True))
    listed = _cli("agents", "status", "--roots")
    pasted = _cli(*shlex.split(forget)[1:], "--yes")

    assert "unknown" not in said and said.endswith(f"`{forget}` forgets it"), said
    assert f"forget it: {forget}" in listed.stdout, listed.stdout
    assert pasted.exit_code == 0, pasted.output
    with store_session() as store:
        assert sandbox_roots.recorded(store) == {}


def test_forgetting_one_root_keeps_every_other(
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
) -> None:
    """MA: one folder and nothing else. Two others recorded, one with a space in its name."""
    kept = [_root(tmp_path / "work"), _root(tmp_path / "my work")]
    gone = _root(tmp_path / "own")

    result = _cli("agents", "disconnect", "codex", "--forget-root", str(gone), "--yes")

    assert result.exit_code == 0, result.output
    with store_session() as store:
        assert sandbox_roots.recorded(store) == {path: "codex" for path in kept}


def test_the_projects_own_codex_session_keeps_the_line_it_had(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
) -> None:
    """MB: a project where a Codex agent ran is said so, before any recorded root around it."""
    _post_checkout(project, tmp_path, "husky")
    _codex_ran_here(project)
    _root(project.root.parent)

    [said] = _said(fleet_service.spawn(project, "coder", worktree=True))

    assert "a Codex agent has run in this project" in said, said
    assert "--forget-root" not in said


@posix
@pytest.mark.parametrize(
    ("typed", "forgets"), [("", False), ("n", False), ("y", True)], ids=["enter", "n", "y"]
)
def test_at_a_terminal_forgetting_is_asked_and_its_default_is_no(
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
    typed: str,
    forgets: bool,
) -> None:
    """MC: a terminal (a pty) is asked `[y/N]`: a bare Enter and "n" forget nothing, "y" forgets.
    The prompt names the agent as the line does."""
    import pty
    import select
    import sys
    import time

    if sys.platform == "win32":  # `posix` skips it; this says so to mypy's win32 pass too
        pytest.skip("no pty on Windows")
    root = _root(tmp_path / "work")
    leader, follower = pty.openpty()
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "aisquare",
            "agents",
            "disconnect",
            "codex",
            "--forget-root",
            str(root),
        ],
        stdin=follower,
        stdout=follower,
        stderr=follower,
        env=dict(os.environ),
        close_fds=True,
    )
    os.close(follower)
    seen = b""

    def read_until(wanted: bytes, seconds: float) -> None:
        nonlocal seen
        deadline = time.monotonic() + seconds
        while wanted not in seen and time.monotonic() < deadline:
            ready, _, _ = select.select([leader], [], [], 0.1)
            if ready:
                try:
                    seen += os.read(leader, 4096)
                except OSError:
                    return

    try:
        read_until(b"[y/N]", 30)
        os.write(leader, f"{typed}\r".encode())
        read_until(b"forgot" if forgets else b"nothing forgotten", 30)
        assert child.wait(timeout=30) == 0, seen
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        os.close(leader)

    said = " ".join(seen.decode(errors="replace").split())
    assert f"Forget that a Codex agent could write {root}?" in said, said
    with store_session() as store:
        assert (root in sandbox_roots.recorded(store)) is not forgets, said


@posix
def test_a_typed_path_is_resolved_before_it_is_matched(
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ME: a link's spelling of a recorded folder (macOS's /var for /private/var), and a quoted
    '~', name the folder they resolve to."""
    real = _root(tmp_path / "real")
    link = tmp_path / "link"
    link.symlink_to(real)
    home = tmp_path / "home"
    at_home = _root(home / "work")
    monkeypatch.setenv("HOME", str(home))
    spelled = [(str(link), real), ("~/work", at_home)]
    if str(real).startswith("/private/var/"):
        spelled.append((str(real).removeprefix("/private"), _root(tmp_path / "var-spelled")))
        spelled[-1] = (str(spelled[-1][1]).removeprefix("/private"), spelled[-1][1])

    for typed, recorded in spelled:
        result = _cli("agents", "disconnect", "codex", "--forget-root", typed, "--yes")
        assert result.exit_code == 0, (typed, result.output)
        with store_session() as store:
            assert recorded not in sandbox_roots.recorded(store), typed
