"""One SKILL.md that PyYAML cannot construct is one invalid persona, never a crash.

Review of #240, finding 4. ``core.personas.parse_skill`` caught ``yaml.YAMLError`` and
nothing else, and PyYAML's safe loader raises more than that: its constructors hand a
scalar to ``datetime``, ``int`` and ``float`` and let their errors through bare, and its
composer recurses once per nesting level. So ``metadata: {reviewed: 2026-09-31}`` in one
directory of any layer (a cloned repository's ``.aisquare/personas`` is enough) raised
``ValueError`` out of ``catalogue()`` and ``resolve()``: ``persona list``, ``show``,
``validate`` and ``import --list`` ended in a traceback, the Personas tab lost its whole
list, and typing such a date into the editor took the Textual app down from the debounced
check.

Every document here is the real thing. The table's cases first show PyYAML raising what
the table says it raises: a document that loaded after all would prove nothing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from textual.pilot import Pilot
from textual.widgets import Button, Static, TextArea
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.cli.ui.persona_dialogs import PARSE_DEBOUNCE, EditPersonaScreen
from aisquare.core import personas as core
from aisquare.core.personas import PersonaError
from aisquare.models import ProjectInfo
from tests import test_persona_cli as cli_suite
from tests import test_ui_personas as ui_suite
from tests.test_persona_cli import BUNDLED
from tests.test_ui_personas import Host, drive, rows, shown

# The suites' fixtures, bound here so pytest finds them for this module's tests.
claude_dir = cli_suite.claude_dir
repo = cli_suite.repo
no_real_tmux = ui_suite.no_real_tmux

IMPOSSIBLE_DATE = "metadata: {reviewed: 2026-09-31}\n"
"""The review's document: September has thirty days, and PyYAML reads a bare date as one."""

NOT_VALID_YAML = "the frontmatter is not valid YAML (ValueError: "
"""What the date costs now: the rule any unparseable frontmatter gets, naming what was raised."""

#: A frontmatter line PyYAML's safe loader cannot construct, and what it raises for it:
#: none of them a ``yaml.YAMLError`` (measured on PyYAML 6.0.3).
UNCONSTRUCTIBLE = [
    pytest.param(IMPOSSIBLE_DATE, ValueError, id="an-impossible-date"),
    pytest.param("reviewed: 2026-02-29\n", ValueError, id="a-leap-day-in-a-common-year"),
    pytest.param("reviewed: !!timestamp soon\n", AttributeError, id="a-timestamp-that-is-not-one"),
    pytest.param("reviewed: !!timestamp {=: 2026-01-01}\n", TypeError, id="a-timestamp-over-a-map"),
    pytest.param("draft: !!bool maybe\n", KeyError, id="a-bool-that-is-not-one"),
    pytest.param('version: !!int ""\n', IndexError, id="an-int-of-no-digits"),
    pytest.param(
        f"nested: {'[' * sys.getrecursionlimit()}{']' * sys.getrecursionlimit()}\n",
        RecursionError,
        id="nesting-past-the-recursion-limit",
    ),
]


def _dated(base: Path, name: str) -> Path:
    """``<base>/<name>``: a skill in every other respect, reviewed on a day that never came."""
    directory = base / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Reviewed on a day that never came.\n"
        f"{IMPOSSIBLE_DATE}---\nWork carefully.\n",
        encoding="utf-8",
    )
    return directory


def _project_layer(repo: Path) -> Path:
    return repo / ".aisquare" / "personas"


# --- the parser ---------------------------------------------------------------------------


@pytest.mark.parametrize(("line", "raised"), UNCONSTRUCTIBLE)
def test_what_pyyaml_cannot_construct_is_refused_as_frontmatter_that_is_not_valid_yaml(
    tmp_path: Path, line: str, raised: type[Exception]
) -> None:
    frontmatter = f"description: fine\n{line}"
    with pytest.raises(raised):  # the premise: PyYAML raises this one bare
        yaml.safe_load(frontmatter)

    with pytest.raises(PersonaError) as caught:
        core.parse_skill(
            f"---\n{frontmatter}---\nbody\n", name="ok", path=tmp_path / "ok", layer="user"
        )

    assert caught.value.code == "not_recognised"
    assert caught.value.line is None  # PyYAML put no mark on it
    assert caught.value.rule.startswith(f"the frontmatter is not valid YAML ({raised.__name__}: ")
    assert str(caught.value).startswith(f"{tmp_path / 'ok' / 'SKILL.md'}: ")


# --- the catalogue and a lookup by name -----------------------------------------------------


def test_a_directory_holding_the_date_is_listed_invalid_and_every_other_persona_still_loads(
    repo: Path,
) -> None:
    bad = _dated(_project_layer(repo), "x")
    cli_suite._skill(_project_layer(repo), "pair", description="Pairs on the work.")
    cli_suite._skill(cli_suite._user_layer(), "mine", description="Mine.")

    personas, invalid = core.catalogue(repo)

    assert [persona.name for persona in personas] == sorted([*BUNDLED, "mine", "pair"])
    assert [path for path, _reason in invalid] == [bad]
    assert invalid[0][1].startswith(NOT_VALID_YAML)
    # A lookup of another persona: beside the bad one, below it, and at the bottom.
    assert core.resolve("pair", repo).layer == "project"
    assert core.resolve("mine", repo).layer == "user"
    assert core.resolve("skeptic", repo).layer == "bundled"


def test_resolving_the_bad_directory_is_a_persona_error_that_lists_the_known_names(
    repo: Path,
) -> None:
    _dated(_project_layer(repo), "x")

    with pytest.raises(PersonaError) as by_its_name:
        core.resolve("x", repo)
    with pytest.raises(PersonaError) as unknown:
        core.resolve("nope", repo)  # the known names are read from the same catalogue

    for caught in (by_its_name, unknown):
        assert caught.value.code == "unknown_persona"
        assert "known: captain, careful, mentor, minimalist, skeptic" in str(caught.value)


def test_a_directory_holding_the_date_never_shadows_the_working_persona_below_it(
    repo: Path,
) -> None:
    _dated(_project_layer(repo), "skeptic")
    _dated(cli_suite._user_layer(), "skeptic")

    assert core.resolve("skeptic", repo).layer == "bundled"


# --- the CLI --------------------------------------------------------------------------------


def test_persona_list_exits_0_and_names_the_directory_that_did_not_load(
    runner: CliRunner, repo: Path
) -> None:
    bad = _dated(_project_layer(repo), "x")

    human = runner.invoke(app, ["persona", "list"])
    machine = runner.invoke(app, ["--json", "persona", "list"])

    assert human.exit_code == 0, repr(human.exception)
    lines = human.stdout.splitlines()
    assert [row.split()[0] for row in lines if not row.startswith("✗")] == BUNDLED
    (refused,) = [row for row in lines if row.startswith("✗")]
    assert refused.startswith(f"✗ {bad}: {NOT_VALID_YAML}")
    assert machine.exit_code == 0, repr(machine.exception)
    payload = json.loads(machine.stdout)
    assert [entry["name"] for entry in payload["personas"]] == BUNDLED
    assert [entry["path"] for entry in payload["invalid"]] == [str(bad)]
    assert payload["invalid"][0]["reason"].startswith(NOT_VALID_YAML)


def test_show_and_validate_answer_in_one_line_never_a_traceback(
    runner: CliRunner, repo: Path
) -> None:
    bad = _dated(_project_layer(repo), "x")

    another = runner.invoke(app, ["persona", "show", "skeptic"])
    by_its_name = runner.invoke(app, ["persona", "show", "x"])
    validated = runner.invoke(app, ["persona", "validate", str(bad)])

    assert another.exit_code == 0, repr(another.exception)
    assert isinstance(by_its_name.exception, SystemExit), repr(by_its_name.exception)
    assert by_its_name.exit_code == 1
    assert "no persona named 'x'" in by_its_name.stderr
    assert isinstance(validated.exception, SystemExit), repr(validated.exception)
    assert validated.exit_code == 1
    assert NOT_VALID_YAML in validated.stderr


def test_import_list_still_lists_the_skills_and_says_which_one_cannot_load(
    runner: CliRunner, repo: Path, claude_dir: Path
) -> None:
    _dated(_project_layer(repo), "x")  # a layer holds the date ...
    _dated(claude_dir / "skills", "dated")  # ... and so does a skill on offer
    cli_suite._skill(claude_dir / "skills", "notes", description="Mine.")

    listed = runner.invoke(app, ["--json", "persona", "import", "--list"])

    assert listed.exit_code == 0, repr(listed.exception)
    by_name = {skill["name"]: skill for skill in json.loads(listed.stdout)["skills"]}
    assert set(by_name) == {"dated", "notes"}
    assert (by_name["notes"]["recognised"], by_name["notes"]["description"]) == (True, "Mine.")
    assert by_name["dated"]["recognised"] is False
    assert by_name["dated"]["reason"].startswith(NOT_VALID_YAML)


# --- the TUI --------------------------------------------------------------------------------


def test_the_personas_tab_marks_the_directory_invalid_and_keeps_every_other_row(
    repo: Path,
) -> None:
    _dated(_project_layer(repo), "x")
    project = ProjectInfo(id="prj_personas", root=repo, codename="amber-otter")

    async def scenario(pilot: Pilot[None], host: Host) -> list[tuple[str, ...]]:
        return rows(host)

    by_name = {row[0]: row for row in drive(scenario, project=project)}
    assert sorted(by_name) == sorted([*BUNDLED, "x"])
    assert (by_name["x"][1], by_name["x"][4]) == ("project", "✗ invalid")
    assert by_name["x"][2].startswith(NOT_VALID_YAML)  # the reason is the row's description


def test_typing_the_date_into_the_editor_is_a_status_line_and_the_app_stays_up(
    repo: Path,
) -> None:
    directory = cli_suite._skill(cli_suite._user_layer(), "pair", description="Pairs.")

    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        dialog = host.screen
        area = dialog.query_one("#edit-text", TextArea)
        area.clear()
        area.insert(f"---\ndescription: Pairs, now.\n{IMPOSSIBLE_DATE}---\nWork.\n")
        await pilot.pause(PARSE_DEBOUNCE + 0.2)  # the debounced check has run
        return [
            type(host.screen).__name__,
            dialog.query_one("#edit-save", Button).disabled,
            shown(dialog.query_one("#edit-status", Static)),
        ]

    screen, save_disabled, status = drive(
        scenario, dialog=EditPersonaScreen("pair", "user", directory, repo)
    )
    assert screen == "EditPersonaScreen"
    assert save_disabled is True
    assert status.startswith(f"✗ {NOT_VALID_YAML}")


def test_the_editor_opens_on_a_skill_md_that_holds_the_date_which_is_how_it_gets_fixed(
    repo: Path,
) -> None:
    bad = _dated(_project_layer(repo), "x")
    mended = (bad / "SKILL.md").read_text(encoding="utf-8").replace("2026-09-31", "2026-09-30")

    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        dialog = host.screen
        area = dialog.query_one("#edit-text", TextArea)
        button = dialog.query_one("#edit-save", Button)
        opened = [button.disabled, shown(dialog.query_one("#edit-status", Static))]
        area.clear()
        area.insert(mended)
        await pilot.pause(PARSE_DEBOUNCE + 0.2)
        return [*opened, button.disabled, shown(dialog.query_one("#edit-status", Static))]

    disabled, status, mended_disabled, mended_status = drive(
        scenario, dialog=EditPersonaScreen("x", "project", bad, repo)
    )
    assert disabled is True and status.startswith(f"✗ {NOT_VALID_YAML}")
    assert mended_disabled is False and mended_status.startswith("✓ parses")
