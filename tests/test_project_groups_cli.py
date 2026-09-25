"""``aisquare project group …``, ``pin`` / ``unpin``, ``move``, ``list --group/--pinned``,
``onboard --group`` (#140): every sidebar gesture has a command that leaves the same store
state, and ``project list --json`` exposes ``group``, ``position`` and ``pinned``."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import paths
from aisquare.core.store import store_session
from aisquare.models import ProjectInfo


@pytest.fixture
def projects(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, str]:
    paths.ensure_home()
    ids: dict[str, str] = {}
    with store_session() as store:
        for name in ("api", "cli", "docs", "web"):
            root = tmp_path / name
            root.mkdir()
            project = store.onboard_project(ProjectInfo(id=f"prj_{name}", root=root))
            ids[name] = project.id
    monkeypatch.chdir(tmp_path / "api")
    return ids


def _listed(runner: CliRunner, *args: str) -> list[dict[str, Any]]:
    result = runner.invoke(app, ["--json", "project", "list", *args])
    assert result.exit_code == 0, result.output
    rows: list[dict[str, Any]] = json.loads(result.stdout)
    return rows


def _names(runner: CliRunner, *args: str) -> list[str]:
    return [row["name"] for row in _listed(runner, *args)]


def test_groups_pins_and_moves_through_the_cli(runner: CliRunner, projects: dict[str, str]) -> None:
    assert _names(runner) == ["api", "cli", "docs", "web"]

    created = runner.invoke(app, ["project", "group", "create", "frontend", "web", "docs"])
    assert created.exit_code == 0, created.output
    assert "✓ group frontend created with 2 project(s)" in created.stdout
    assert _names(runner) == ["web", "docs", "api", "cli"], "grouped first, then the loose ones"
    rows = {row["name"]: row for row in _listed(runner)}
    assert rows["web"]["group"] == "frontend" and rows["web"]["position"] == 0
    assert rows["docs"]["position"] == 1 and rows["api"]["group"] is None
    assert rows["api"]["pinned"] is False

    assert _names(runner, "--group", "frontend") == ["web", "docs"]
    missing = runner.invoke(app, ["--json", "project", "list", "--group", "nope"])
    assert missing.exit_code == 1 and json.loads(missing.stdout)["error"] == "not_found"

    added = runner.invoke(app, ["project", "group", "add", "frontend", "cli"])
    assert added.exit_code == 0 and _names(runner, "--group", "frontend") == ["web", "docs", "cli"]
    moved = runner.invoke(app, ["project", "move", "cli", "--before", "web"])
    assert moved.exit_code == 0, moved.output
    assert _names(runner, "--group", "frontend") == ["cli", "web", "docs"]
    out = runner.invoke(app, ["project", "move", "docs", "--to", "top", "--position", "0"])
    assert out.exit_code == 0 and _names(runner) == ["cli", "web", "docs", "api"]
    removed = runner.invoke(app, ["project", "group", "remove", "cli"])
    assert removed.exit_code == 0 and _names(runner) == ["web", "docs", "api", "cli"]

    pinned = runner.invoke(app, ["project", "pin", "api"])
    assert pinned.exit_code == 0 and _names(runner) == ["api", "web", "docs", "cli"]
    assert _names(runner, "--pinned") == ["api"]
    table = runner.invoke(app, ["project", "list"])
    assert "📌" in table.stdout and "GROUP" in table.stdout and "frontend" in table.stdout
    unpinned = runner.invoke(app, ["project", "unpin", "api"])
    assert unpinned.exit_code == 0 and _names(runner, "--pinned") == []

    renamed = runner.invoke(app, ["project", "group", "rename", "frontend", "ui"])
    assert renamed.exit_code == 0
    dup = runner.invoke(app, ["project", "group", "create", "UI"])
    assert dup.exit_code == 1 and "already exists" in dup.output
    second = runner.invoke(app, ["project", "group", "create", "tools"])
    assert second.exit_code == 0
    reordered = runner.invoke(app, ["project", "group", "move", "tools", "--before", "ui"])
    assert reordered.exit_code == 0
    listing = runner.invoke(app, ["--json", "project", "group", "list"])
    payload = json.loads(listing.stdout)
    assert [g["name"] for g in payload["groups"]] == ["tools", "ui"]
    assert [m["name"] for m in payload["groups"][1]["members"]] == ["web"]
    plain = runner.invoke(app, ["project", "group", "list"])
    assert "ui: web" in plain.stdout and "tools: —" in plain.stdout

    deleted = runner.invoke(app, ["project", "group", "delete", "ui"])
    assert deleted.exit_code == 0 and "its projects are back at the top level" in deleted.stdout
    assert _names(runner) == ["docs", "api", "cli", "web"]  # ungrouped to the end, no project lost
    assert runner.invoke(app, ["project", "group", "delete", "ui"]).exit_code == 1


def test_onboard_into_a_group_creates_it_when_new(
    runner: CliRunner, projects: dict[str, str], tmp_path: Path
) -> None:
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    result = runner.invoke(app, ["project", "onboard", str(fresh), "--group", "new-things"])
    assert result.exit_code == 0, result.output
    assert _names(runner, "--group", "new-things") == ["fresh"]
    again = runner.invoke(
        app, ["project", "onboard", str(tmp_path / "web"), "--group", "new-things"]
    )
    assert again.exit_code == 0, again.output
    assert _names(runner, "--group", "new-things") == ["fresh", "web"]
    listing = json.loads(runner.invoke(app, ["--json", "project", "group", "list"]).stdout)
    assert [g["name"] for g in listing["groups"]] == ["new-things"], "created once, reused after"


def test_onboard_into_a_new_group_the_store_refuses_leaves_no_empty_group(
    runner: CliRunner,
    projects: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``onboard --group`` created a new group in one transaction and added the project
    in another, so an add the store refused left an empty group behind (review of #203).
    The group is made with its member, or not at all."""
    from aisquare.services import project_groups

    def refused(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.setattr(project_groups, "move_project", refused)
    result = runner.invoke(app, ["project", "onboard", str(fresh), "--group", "new-things"])
    assert result.exit_code != 0
    listing = json.loads(runner.invoke(app, ["--json", "project", "group", "list"]).stdout)
    assert [g["name"] for g in listing["groups"]] == [], "an empty group was left behind"


def test_onboard_into_a_blank_group_is_refused_before_the_onboard(
    runner: CliRunner, projects: dict[str, str], tmp_path: Path
) -> None:
    """``onboard --group ' '`` found no group by that name and asked ``create_group`` for
    one, whose ``ValueError("a group needs a name")`` escaped uncaught, after the onboard
    had committed (review of #203, round 2). It is refused as ``group create ' '`` is,
    and nothing is onboarded."""
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    result = runner.invoke(app, ["--json", "project", "onboard", str(fresh), "--group", " "])
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit), result.output
    assert json.loads(result.stdout)["error"] == "invalid_group"
    assert "fresh" not in _names(runner, "--all"), "onboarded before the group was refused"


def test_onboard_into_a_group_named_with_spaces_joins_the_group_of_that_name(
    runner: CliRunner,
    projects: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``onboard --group ' team '`` looked the group up as typed and found none, and
    ``create_group``, which strips, collided with ``team``: its ``ValueError`` escaped
    uncaught after the onboard had committed (review of #203, round 3). The name is read
    as ``group create`` stores it, so the project joins ``team``. A group the store still
    refuses to create (made meanwhile by another command) is ``invalid_group``, as
    ``group create`` says it, not a traceback."""
    from aisquare.services import project_groups

    assert runner.invoke(app, ["project", "group", "create", "team"]).exit_code == 0
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    result = runner.invoke(app, ["--json", "project", "onboard", str(fresh), "--group", " team "])
    assert result.exit_code == 0, result.output
    assert _names(runner, "--group", "team") == ["fresh"]

    def made_meanwhile(_store: object, ref: str) -> None:
        raise KeyError(ref)

    monkeypatch.setattr(project_groups, "resolve_group", made_meanwhile)
    again = runner.invoke(
        app, ["--json", "project", "onboard", str(tmp_path / "web"), "--group", "team"]
    )
    assert again.exit_code == 1 and isinstance(again.exception, SystemExit), again.output
    assert json.loads(again.stdout)["error"] == "invalid_group"


def test_a_filter_that_matches_nothing_says_so_not_that_nothing_is_registered(
    runner: CliRunner, projects: dict[str, str]
) -> None:
    """``--pinned`` with nothing pinned, ``--group`` on an empty group: the list has rows,
    and the empty table said "No projects registered yet. Run: aisquare init" (review of
    #171, round 1). It names the filter and the step that fills it; ``--json`` is ``[]``."""

    def shown(*args: str) -> str:
        result = runner.invoke(app, ["project", "list", *args])
        assert result.exit_code == 0, result.output
        return " ".join(result.stdout.split())

    pinned = shown("--pinned")
    assert pinned == "No pinned projects — pin one: aisquare project pin <project>", pinned
    assert runner.invoke(app, ["project", "group", "create", "empty set"]).exit_code == 0
    grouped = shown("--group", "empty set")
    assert grouped == (
        "No projects in group empty set — add one: aisquare project group add 'empty set' <project>"
    ), grouped
    assert runner.invoke(app, ["project", "group", "create", "site", "web"]).exit_code == 0
    both = shown("--group", "site", "--pinned")
    assert both.startswith("No pinned projects in group site — pin one:"), both
    assert _listed(runner, "--pinned") == [] and _listed(runner, "--group", "empty set") == []
    # A list with nothing in it at all still says so, filter or not.
    for project_id in projects.values():
        assert runner.invoke(app, ["project", "forget", project_id]).exit_code == 0
    assert "No projects registered yet" in shown("--pinned")


def test_a_filter_whose_matches_are_hidden_points_to_them_not_to_a_step_already_taken(
    runner: CliRunner, projects: dict[str, str], tmp_path: Path
) -> None:
    """A captured directory can be grouped and pinned (the sidebar's `a` arranges it too),
    and the list hides it (#139). ``--group`` and ``--pinned`` then matched nothing listed,
    and the empty table said "add one: … group add exp <project>" or "pin one" — a step
    already taken, which changed nothing taken again (review of #171, round 2). It counts
    the hidden matches and points to ``--all`` with the same filter."""

    def shown(*args: str) -> str:
        result = runner.invoke(app, ["project", "list", *args])
        assert result.exit_code == 0, result.output
        return " ".join(result.stdout.split())

    with store_session() as store:
        store.ensure_project(ProjectInfo(id="prj_scratch", root=tmp_path / "scratch"))
    assert runner.invoke(app, ["project", "group", "create", "exp", "prj_scratch"]).exit_code == 0
    grouped = shown("--group", "exp")
    assert grouped == (
        "No listed projects in group exp — 1 captured directory hidden (a hooked session ran "
        "there): aisquare project list --all --group exp; add one: aisquare project onboard "
        "<path>"
    ), grouped
    assert [row["name"] for row in _listed(runner, "--all", "--group", "exp")] == ["scratch"]
    assert runner.invoke(app, ["project", "pin", "prj_scratch"]).exit_code == 0
    pinned = shown("--pinned")
    assert pinned.startswith("No listed pinned projects — 1 captured directory hidden"), pinned
    assert pinned.endswith(
        "aisquare project list --all --pinned; add one: aisquare project onboard <path>"
    )
    both = shown("--pinned", "--group", "exp")
    assert "aisquare project list --all --pinned --group exp;" in both, both
    # With nothing hidden that matches, the step that fills the filter is still named.
    assert "scratch" in shown("--pinned", "--group", "exp", "--all")
    assert runner.invoke(app, ["project", "group", "create", "site"]).exit_code == 0
    assert shown("--group", "site").startswith("No projects in group site — add one:")


def test_the_step_named_for_an_empty_group_reads_a_dashed_name_as_the_group(
    runner: CliRunner, projects: dict[str, str]
) -> None:
    """``shlex.quote`` keeps ``-wip`` one word, and Click still read it as options: the step
    the empty table named exited 2 with "No such option: -w" (review of #171, round 2).
    ``--`` ends the options first, and the step it names works as printed."""
    assert runner.invoke(app, ["project", "group", "create", "--", "-wip"]).exit_code == 0
    result = runner.invoke(app, ["project", "list", "--group", "-wip"])
    assert result.exit_code == 0, result.output
    said = " ".join(result.stdout.split())
    assert (
        said == "No projects in group -wip — add one: aisquare project group add -- -wip <project>"
    )
    step = said.split(": aisquare ", 1)[1].replace("<project>", "web").split()
    added = runner.invoke(app, step)
    assert added.exit_code == 0, added.output
    assert _names(runner, "--group", "-wip") == ["web"]
