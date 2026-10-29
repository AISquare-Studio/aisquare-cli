"""A project renamed carries its fleet codename and tmux session with it (task #6).

The card showed the nickname or the folder ("aisquare-cli") while the fleet called the
project by a generated codename ("earnest-hawk") nothing in the TUI showed, and a rename
changed the nickname alone. So a manager's "earnest-hawk …" named nothing the owner could
see. Now a rename — ``project rename``, a card, the Settings tab — sets the codename to the
slug of the name (:func:`codenames.codename_from_text`) and renames the tmux session; the
old codename still finds the project; the boards its agents read are told; nothing live
breaks, and no branch or worktree is renamed.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import codenames
from aisquare.core.codenames import CODENAME_MAX, codename_from_text, first_free, is_codename
from aisquare.core.orchestrator import team_project
from aisquare.core.store import SqliteStore, store_session
from aisquare.models import ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import project as project_service
from aisquare.services.fleet import FleetError
from tests import test_fleet_service as fleet_suite
from tests.test_fleet_service import (
    FakeTmux,
    _add_task,
    _board_session,
    _codename,
    _commit,
    _events,
    _git,
)

# The fleet suite's fixtures, bound here so pytest finds them for this module's tests.
tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project


def _repo(tmp_path: Path, name: str) -> ProjectInfo:
    """Another project added on purpose, a git checkout of its own at ``tmp_path/name``."""
    root = tmp_path / name
    root.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=root)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=root)
    info = team_project(root)
    with store_session() as store:
        return store.onboard_project(info)


def _stored(project: ProjectInfo) -> ProjectInfo:
    with store_session() as store:
        found = store.get_project(project.id)
    assert found is not None
    return found


def _renamed_notes(project: ProjectInfo) -> list[str]:
    return [text for text in _events(project, "note") if text.startswith("project renamed:")]


# --- 1. the slug ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "slug"),
    [
        ("AISquare CLI", "aisquare-cli"),
        ("Café ✨ Board", "cafe-board"),
        ("!!!", None),
        ("aisquare-cli", "aisquare-cli"),
        ("earnest-hawk", "earnest-hawk"),
        ("  Billing   v2  ", "billing-v2"),
        ("fix: the login loop (again!) — #42 [wip]", "fix-the-login-loop-again"),
        ("2024 Roadmap", "roadmap"),
        ("Über.Proj:ect..x", "uber-proj-ect-x"),
        ("naïve façade", "naive-facade"),
        ("日本語のプロジェクト 🚀", None),
        ("42", None),
        ("---", None),
        ("", None),
        ("PRJ", None),
        ("x" * 40, "x" * CODENAME_MAX),
        ("a" * 23 + " b", "a" * 23),
    ],
)
def test_the_slug_of_a_name(text: str, slug: str | None) -> None:
    """Lowercased, accents folded, every run outside ``[a-z0-9]`` one ``-``, trimmed to start
    with a letter and to 24 characters with no ``-`` at an end. ``prj`` gives none: every
    project id starts ``prj_``, so ``--project prj`` could never name it."""
    assert codename_from_text(text) == slug


@pytest.mark.parametrize(
    "text",
    [
        "AISquare CLI",
        "Café ✨ Board",
        "a..b",
        "a.lock",
        "x:y/z\\w",
        "tab\there",
        "@{u}",
        "-leading and trailing-",
        "UPPER lower 123 mixed",
        "ʀᴇᴀʟʟʏ ᴡᴇɪʀᴅ",
        "étude",
        "z" * 60 + "!",
    ],
)
def test_a_slug_names_a_tmux_session_and_a_git_ref(text: str) -> None:
    """No ``.`` or ``:`` (tmux target separators), no ``..``, space or ``/``; asked of git
    itself as the middle of a fleet branch."""
    slug = codename_from_text(text)
    if slug is None:
        return
    assert is_codename(slug)
    assert re.fullmatch(r"[a-z][a-z0-9]*(-[a-z0-9]+)*", slug) and len(slug) <= CODENAME_MAX
    _git("check-ref-format", "--branch", f"fleet/{slug}/01abc-work", cwd=Path.cwd())


def test_the_wider_shape_is_what_a_chosen_codename_may_have() -> None:
    for good in ("aisquare-cli", "amber-otter", "web", "billing-v2", "a1-b2-c3", "x" * 24):
        assert is_codename(good), good
    for bad in (
        "Amber-Otter",
        "amber_otter",
        "amber otter",
        "amber--otter",
        "-amber",
        "amber-",
        "1amber",
        "amber.otter",
        "amber:otter",
        "x" * 25,
        "p",
        "pr",
        "prj",
        "",
    ):
        assert not is_codename(bad), bad
    # Generated codenames are still two curated words: the picker is untouched.
    assert codenames.CODENAME.match(codenames.codename_for("prj_0123456789abcdef"))


def test_a_taken_slug_gets_the_next_free_suffix_within_the_bound() -> None:
    assert first_free("aisquare-cli", set()) == "aisquare-cli"
    assert first_free("aisquare-cli", {"aisquare-cli"}) == "aisquare-cli-2"
    assert first_free("aisquare-cli", {"aisquare-cli", "aisquare-cli-2"}) == "aisquare-cli-3"
    long = "a" * 21 + "-bb"  # 24 characters
    assert first_free(long, {long}) == "a" * 21 + "-2", "cut to fit, no '-' left before it"
    assert all(len(first_free("y" * 24, {"y" * 24})) <= CODENAME_MAX for _ in range(3))


def test_fleet_rename_accepts_the_wider_shape(tmux: FakeTmux, project: ProjectInfo) -> None:
    fleet_service.ensure_codename(project)
    assert fleet_service.rename(project, "aisquare-cli").codename == "aisquare-cli"
    with pytest.raises(FleetError, match="lowercase letters and digits"):
        fleet_service.rename(project, "Not.A.Codename")
    assert _codename(project) == "aisquare-cli"


def test_a_name_that_gives_no_codename_saves_the_nickname_and_says_so(
    tmux: FakeTmux, project: ProjectInfo
) -> None:
    before = fleet_service.ensure_codename(project).codename
    notes: list[str] = []
    renamed = fleet_service.rename_project(project, "!!!", notes=notes)
    assert renamed.nickname == "!!!" and _stored(project).nickname == "!!!"
    assert renamed.codename == before == _codename(project), "the codename is left as it was"
    [note] = notes
    assert "'!!!' gives no codename" in note and f"stays {before}" in note
    assert f"aisquare fleet rename --project {before} <codename>" in note
    assert tmux.renamed == [] and _renamed_notes(project) == []


# --- 2. the rename carries through ------------------------------------------------------


def test_a_rename_sets_the_nickname_the_codename_and_the_tmux_session(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    fleet_service.spawn(project, "manager")
    old = _codename(project)
    notes: list[str] = []

    renamed = fleet_service.rename_project(project, "AISquare CLI", notes=notes)

    assert (renamed.nickname, renamed.codename) == ("AISquare CLI", "aisquare-cli")
    assert _stored(project).codename == "aisquare-cli"
    assert tmux.renamed == [(f"asq-{old}", "asq-aisquare-cli")]
    assert notes == []
    [status] = fleet_service.list_agents(project)
    assert status.tmux_session == "asq-aisquare-cli" and status.state == "waiting"


def test_clearing_the_name_takes_the_codename_from_the_folder(
    tmux: FakeTmux, project: ProjectInfo
) -> None:
    fleet_service.rename_project(project, "Billing v2")
    assert _codename(project) == "billing-v2"
    cleared = fleet_service.rename_project(project, None)
    assert cleared.nickname is None and cleared.codename == codename_from_text(project.root.name)
    assert cleared.codename == "repo"


def test_a_project_with_no_codename_yet_is_given_its_names(
    tmux: FakeTmux, project: ProjectInfo
) -> None:
    """No agent ever ran there: its first one is named what its card says."""
    with store_session() as store:
        assert store.get_project(project.id).codename is None  # type: ignore[union-attr]
    assert fleet_service.rename_project(project, "Auth rewrite").codename == "auth-rewrite"
    assert tmux.renamed == [] and _renamed_notes(project) == [], "nobody held the old one"


def test_a_slug_another_project_holds_gets_a_suffix_and_the_owner_is_told(
    tmux: FakeTmux, project: ProjectInfo, tmp_path: Path
) -> None:
    other = _repo(tmp_path, "elsewhere/aisquare-cli")
    fleet_service.rename_project(other, "Other checkout")
    fleet_service.rename(other, "aisquare-cli")
    notes: list[str] = []
    renamed = fleet_service.rename_project(project, "AISquare CLI", notes=notes)
    assert renamed.codename == "aisquare-cli-2"
    assert notes == [
        "aisquare-cli is Other checkout's codename, so this project's is aisquare-cli-2"
    ]
    # Saved again under the same name, it keeps its suffix rather than walking further.
    again: list[str] = []
    assert fleet_service.rename_project(project, "AISquare CLI", notes=again).codename == (
        "aisquare-cli-2"
    )
    # Freed, the name is taken back at the next rename.
    fleet_service.rename(other, "other-checkout")
    assert fleet_service.rename_project(project, "AISquare CLI").codename == "aisquare-cli"


def test_a_codename_half_that_fails_leaves_the_nickname_saved_and_says_why(
    tmux: FakeTmux, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(project: ProjectInfo, *, notes: list[str] | None = None) -> ProjectInfo:
        raise FleetError("every free name this walk tried was taken")

    monkeypatch.setattr(fleet_service, "follow_name", refuse)
    notes: list[str] = []
    renamed = fleet_service.rename_project(project, "Payments", notes=notes)
    assert renamed.nickname == "Payments" == _stored(project).nickname
    assert notes == [
        "the fleet codename was not changed: every free name this walk tried was taken"
    ]


def test_a_store_race_for_the_slug_walks_on_to_the_next(
    tmux: FakeTmux, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Another process takes ``billing`` between the read and the write: the index refuses
    it, and the walk lands one suffix on."""
    other = _repo(tmp_path, "other")
    real = SqliteStore.set_codename
    raced: list[str] = []

    def racing(store: SqliteStore, project_id: str, codename: str) -> ProjectInfo:
        if project_id == project.id and not raced:
            raced.append(codename)
            real(store, other.id, codename)  # the other process wins
            raise sqlite3.IntegrityError("UNIQUE constraint failed: project.codename")
        return real(store, project_id, codename)

    monkeypatch.setattr(SqliteStore, "set_codename", racing)
    project_service.set_nickname(project, "Billing")
    assert fleet_service.follow_name(project).codename == "billing-2"
    assert raced == ["billing"] and _codename(other) == "billing"


def test_project_rename_says_the_codename_and_the_old_one_that_still_works(
    tmux: FakeTmux, project: ProjectInfo
) -> None:
    old = fleet_service.ensure_codename(project).codename
    assert old is not None
    runner = CliRunner()
    result = runner.invoke(app, ["project", "rename", "AISquare CLI", "-P", project.id])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "✓ repo is now called AISquare CLI",
        f"  fleet codename: aisquare-cli (tmux asq-aisquare-cli), was {old} "
        f"(--project {old} still works)",
    ]
    payload = json.loads(
        runner.invoke(app, ["--json", "project", "rename", "Café ✨ Board", "-P", old]).stdout
    )
    assert (payload["id"], payload["codename"], payload["notes"]) == (
        project.id,
        "cafe-board",
        [],
    )
    bang = runner.invoke(app, ["project", "rename", "!!!", "-P", "cafe-board"])
    assert bang.exit_code == 0, bang.output
    assert "⚠ '!!!' gives no codename" in bang.stdout and "stays cafe-board" in bang.stdout


# --- 3. the old names keep working -------------------------------------------------------


def test_the_old_codename_still_finds_the_project_and_the_board_is_told_once(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    fleet_service.spawn(project, "manager")
    old = _codename(project)
    renamed = fleet_service.rename_project(project, "AISquare CLI")

    for ref in (old, "aisquare-cli"):
        assert project_service.resolve(ref).id == project.id, ref
        assert fleet_service.resolve_project(ref).id == project.id, ref
    assert _renamed_notes(project) == [
        f"project renamed: {old} → aisquare-cli; --project {old} still works"
    ]
    # Every hint printed from now on names the new codename.
    assert project_service.fleet_command("ls", renamed).endswith("--project aisquare-cli")
    assert fleet_service.attach_argv(project)[-1] == "=asq-aisquare-cli"
    # A second rename keeps both earlier names, and tells the board once more.
    fleet_service.rename_project(project, "Billing")
    assert {project_service.resolve(ref).id for ref in (old, "aisquare-cli", "billing")} == {
        project.id
    }
    assert len(_renamed_notes(project)) == 2


def test_a_project_that_holds_an_old_codename_now_is_the_one_it_names(
    tmux: FakeTmux, project: ProjectInfo, tmp_path: Path
) -> None:
    old = fleet_service.ensure_codename(project).codename
    assert old is not None
    fleet_service.rename_project(project, "Renamed")
    other = _repo(tmp_path, "other")
    assert project_service.resolve(old).id == project.id, "the alias, while nobody holds it"
    fleet_service.rename(other, old)
    assert project_service.resolve(old).id == other.id, "the current holder wins"
    assert fleet_service.resolve_project(old).id == other.id
    # Taken back by its old project, it is that project's current codename again.
    fleet_service.rename(other, "someone-else")
    fleet_service.rename(project, old)
    with store_session() as store:
        assert old not in store.codename_aliases(project.id)
    assert project_service.resolve("renamed").id == project.id


def test_an_exact_codename_wins_over_another_checkouts_folder_name(
    tmux: FakeTmux, project: ProjectInfo, tmp_path: Path
) -> None:
    """Every checkout of a repository has its folder's name: renamed "AISquare CLI", one of
    them is ``aisquare-cli``, and the hints printed for it say ``--project aisquare-cli``."""
    twin = _repo(tmp_path, "ws6/aisquare-cli")
    assert fleet_service.rename_project(project, "AISquare CLI").codename == "aisquare-cli"
    assert project_service.resolve("aisquare-cli").id == project.id
    assert fleet_service.resolve_project("aisquare-cli").id == project.id
    assert project_service.resolve(str(twin.root)).id == twin.id, "the twin by its path"


def test_a_forgotten_projects_old_codename_names_nothing_and_a_purge_takes_it(
    tmux: FakeTmux, project: ProjectInfo
) -> None:
    old = fleet_service.ensure_codename(project).codename
    assert old is not None
    fleet_service.rename_project(project, "Gone soon")
    project_service.forget(project.id)
    with pytest.raises(KeyError):
        project_service.resolve(old)
    with store_session() as store:
        assert store.codename_alias_owners() == {old: project.id}, "kept with the tombstone"
        store.purge_project(project.id)
        assert store.codename_alias_owners() == {}


# --- 4. nothing live breaks --------------------------------------------------------------


def test_a_worktree_agent_survives_the_rename_and_its_task_carries_on_its_old_branch(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Spawn a worktree coder for a task, rename the project, then tell, stop and reap it:
    each finds it by its pane, under the renamed session. Its branch and worktree keep
    their names, and a coder spawned again for its task checks the old branch out, commits
    and all, rather than cutting a new one under the new codename from main."""
    task = _add_task(project, "Wire the auth flow")
    fleet_service.spawn(project, "manager")
    coder = fleet_service.spawn(project, "coder", task_id=task.id).agent
    old = _codename(project)
    _commit(coder.cwd, "feature.txt")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=coder.cwd)
    tip = _git("rev-parse", "HEAD", cwd=coder.cwd)
    assert branch.startswith(f"fleet/{old}/")

    fleet_service.rename_project(project, "AISquare CLI")
    assert tmux.renamed == [(f"asq-{old}", "asq-aisquare-cli")]
    assert coder.cwd.exists() and _git("branch", "--list", branch, cwd=project.root)

    _board_session(coder, "waiting")
    tmux.set_command(coder.pane_id, "claude")  # the agent, not the launcher, is up
    told = fleet_service.tell(project, coder.label, "rebase onto main")
    assert told.delivered and (coder.pane_id, "paste", "rebase onto main") in tmux.typed
    statuses = {s.agent.label: s for s in fleet_service.list_agents(project)}
    assert statuses[coder.label].tmux_session == "asq-aisquare-cli"
    fleet_service.stop(project, coder.label, force=True)
    fleet_service.reap(project)
    assert _git("branch", "--list", branch, cwd=project.root), "unmerged: kept"

    again = fleet_service.spawn(project, "coder", task_id=task.id)
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=again.agent.cwd) == branch
    assert _git("rev-parse", "HEAD", cwd=again.agent.cwd) == tip, "its own commits"
    assert not _git("branch", "--list", "fleet/aisquare-cli/*", cwd=project.root)
    assert again.tmux_session == "asq-aisquare-cli"


def test_a_spawn_from_a_copy_read_before_the_rename_uses_the_new_session(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """A Spawn dialog left open across a rename holds the old codename: the spawn names the
    session by the one the row holds now, and tmux makes no ``asq-<old>`` beside it."""
    stale = fleet_service.ensure_codename(project)
    fleet_service.spawn(project, "manager")
    fleet_service.rename_project(project, "Fresh name")
    receipt = fleet_service.spawn(stale, "coder", worktree=False)
    assert receipt.tmux_session == "asq-fresh-name"
    assert set(tmux.sessions) == {"asq-fresh-name"}


def test_the_session_is_renamed_on_every_server_the_rows_recorded(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent spawned before ``[fleet] tmux_socket`` changed lives on the old socket, which
    its row recorded: renamed on today's socket alone, its session kept the old name there,
    and ``attach`` and a scoped ``shutdown`` then looked for the new one."""
    fleet_service.spawn(project, "manager")
    old = _codename(project)
    moved = FakeTmux()
    tmux.per_socket["asq-moved"] = moved
    fleet_suite._settings(monkeypatch, tmux_socket="asq-moved")

    fleet_service.rename_project(project, "Moved")

    assert tmux.renamed == [(f"asq-{old}", "asq-moved")], "on the server the row recorded"
    assert moved.renamed == [], "today's socket holds no such session: asked, not renamed"
    assert fleet_service.attach_argv(project)[-1] == "=asq-moved"


# --- 5. the card says the codename where its name does not --------------------------------


def _card_project(
    name: str | None, codename: str | None, folder: str = "aisquare-cli"
) -> ProjectInfo:
    return ProjectInfo(id="prj_x", root=Path("/w", folder), nickname=name, codename=codename)


@pytest.mark.parametrize(
    ("nickname", "codename", "aside"),
    [
        (None, "earnest-hawk", "earnest-hawk"),  # generated, never renamed
        (None, "aisquare-cli", None),  # the folder's own slug
        ("AISquare CLI", "aisquare-cli", None),  # renamed: the name says it
        ("AISquare CLI", "aisquare-cli-2", "aisquare-cli-2"),  # suffixed: another held it
        ("AISquare CLI", "billing", "billing"),  # set with `fleet rename`
        ("!!!", "earnest-hawk", "earnest-hawk"),  # a name that gives no codename
        ("Anything", None, None),  # no agent yet: nothing to say
    ],
)
def test_the_card_says_the_codename_when_its_name_does_not_give_it(
    nickname: str | None, codename: str | None, aside: str | None
) -> None:
    from collections import Counter

    from aisquare.cli.ui.sidebar import (
        codename_aside,
        project_name,
        project_subtitle,
        project_title_tooltip,
        short_path,
    )

    shown = _card_project(nickname, codename)
    assert codename_aside(shown) == aside
    alone = Counter([project_name(shown)])
    twins = Counter([project_name(shown)] * 2)
    path = short_path(shown.root)
    assert project_subtitle(shown, alone, always=False) == (f"· {aside}" if aside else None)
    assert project_subtitle(shown, twins, always=False) == (f"{path} · {aside}" if aside else path)
    tooltip = project_title_tooltip(shown)
    assert "double-click or F2 to rename" in tooltip
    if codename:
        assert f"fleet codename {codename} (tmux asq-{codename}, --project {codename})" in tooltip
    else:
        assert tooltip.startswith("no fleet codename yet")
