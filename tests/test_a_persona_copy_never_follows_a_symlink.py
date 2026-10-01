"""``persona import`` and ``persona export`` never follow a symbolic link: they copy the
rest of the skill directory and say which links they left out.

Review of #240, finding 15. Both copied with ``shutil.copytree``'s default, which follows
every link in the tree and writes what it points at as a regular file. A cloned
repository's ``.claude/skills/helper`` holding ``env.txt -> /proc/self/environ`` or
``refs -> ../../../../.ssh`` was offered by ``persona import --list``, and ``persona import
helper --project`` wrote the process's environment, or the owner's keys, into
``<repo>/.aisquare/personas/helper``: the layer that is committed. ``persona export`` wrote
them out again.

The owner's ruling: skip and warn. A link anywhere in the tree, to a file or to a
directory, wherever it points, is left out of the copy, link and target both; everything
else is copied as before. The result lists what was skipped, the CLI prints one warning
line naming it, ``--json`` carries it, and the Import, Export and Save as dialogs show the
same sentence. One link cannot be skipped: a SKILL.md that is itself a link is refused,
since there is no persona without it and following it would read a file outside the skill.

The links are real ones, made with ``os.symlink`` under ``tmp_path``; where this machine
cannot make one (Windows without the privilege) the test skips. The secret is a fake.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, NoReturn

import pytest
from textual.pilot import Pilot
from textual.widgets import Input, RadioButton
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.cli.ui.persona_dialogs import (
    EditPersonaScreen,
    ExportDone,
    ExportPersonaScreen,
    ImportPersonaScreen,
)
from aisquare.core import personas as core
from aisquare.core.personas import Layer, PersonaError, Provenance
from aisquare.services import persona_import
from aisquare.services import personas as service
from tests import test_persona_cli as cli_suite
from tests import test_ui_personas as ui_suite
from tests.fsperms import can_symlink
from tests.test_ui_personas import Host, Recorder, drive, press, settle

# The suites' fixtures, bound here so pytest finds them for this module's tests.
claude_dir = cli_suite.claude_dir
repo = cli_suite.repo
no_real_tmux = ui_suite.no_real_tmux
skills = ui_suite.skills

SECRET = b"AISQUARE_FAKE_SECRET=not-a-real-key-4f1d9c2a\n"
"""What the links point at. A fake: nothing here is anyone's credential."""

CODE = "symlink_refused"
REMEDY = "replace it with a real file"

Plant = Callable[[Path, Path], str]
"""Puts one link into a skill directory (given the vault) and returns its relative path."""


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A directory outside the repository and every layer, standing for ``~/.ssh``."""
    directory = tmp_path / "outside" / "ssh"
    directory.mkdir(parents=True)
    (directory / "id_ed25519").write_bytes(SECRET)
    return directory


def _link(link: Path, target: Path, *, directory: bool = False) -> None:
    """A real symbolic link at ``link``, or a skip where this machine cannot create one:
    Windows without the privilege. Where it can, a failure here is the test's own and is
    raised: a skip would report "cannot" for a link this module planted twice."""
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        if can_symlink():
            raise
        pytest.skip(f"this machine cannot create a symbolic link ({type(exc).__name__}: {exc})")


def _skill(base: Path) -> Path:
    """``<base>/helper``: a real skill with one real supporting file."""
    return cli_suite._skill(
        base, "helper", description="Helps.", files={"references/guide.md": b"guide\n"}
    )


def _to_a_file(skill: Path, vault: Path) -> str:
    _link(skill / "env.txt", vault / "id_ed25519")
    return "env.txt"


def _to_a_directory(skill: Path, vault: Path) -> str:
    _link(skill / "refs", vault, directory=True)
    return "refs"


def _relative_and_out_of_the_repository(skill: Path, vault: Path) -> str:
    # The review's own shape, ``refs -> ../../../../.ssh``: four levels up is tmp_path.
    _link(skill / "refs", Path("..", "..", "..", "..", "outside", "ssh"), directory=True)
    return "refs"


def _three_levels_down(skill: Path, vault: Path) -> str:
    (skill / "references" / "deep" / "down").mkdir(parents=True)
    _link(skill / "references" / "deep" / "down" / "key", vault / "id_ed25519")
    return "references/deep/down/key"


def _to_nowhere(skill: Path, vault: Path) -> str:
    _link(skill / "gone.txt", vault / "never-was")
    return "gone.txt"


def _that_stays_inside_the_skill(skill: Path, vault: Path) -> str:
    _link(skill / "alias.md", Path("references", "guide.md"))
    return "alias.md"


def _in_place_of_the_skill_md(skill: Path, vault: Path) -> str:
    """The one link that cannot be skipped: SKILL.md itself, pointing at a real skill file."""
    real = vault.parent / "SKILL.md"
    (skill / "SKILL.md").replace(real)
    _link(skill / "SKILL.md", real)
    return "SKILL.md"


LINKS = [
    pytest.param(_to_a_file, id="to-a-file"),
    pytest.param(_to_a_directory, id="to-a-directory"),
    pytest.param(_relative_and_out_of_the_repository, id="relative-and-out-of-the-repository"),
    pytest.param(_three_levels_down, id="three-levels-down"),
    pytest.param(_to_nowhere, id="to-nowhere"),
    pytest.param(_that_stays_inside_the_skill, id="that-stays-inside-the-skill"),
]


def _never_asked(_draft: service.PersonaDraftView) -> bool:
    raise AssertionError("the copy path never asks for a confirmation")


def _import(
    source: str,
    *,
    root: Path | None,
    layer: Layer = "project",
    force: bool = False,
    llm: Literal["auto", "always", "never"] = "auto",
    condense: bool = False,
) -> service.ImportResult:
    return service.import_source(
        source,
        layer=layer,
        root=root,
        name=None,
        force=force,
        llm=llm,
        condense=condense,
        engine=None,
        model=None,
        confirm=_never_asked,
    )


def _export(repo: Path, to: Path) -> service.ExportResult | str:
    """``helper`` exported to ``to``. What came back is the caller's to check, after it has
    looked at what was written: a type that is wrong says less than a secret that leaked."""
    return service.export("helper", root=repo, to=to, skill=None, force=False)


def _refused(call: Callable[[], object]) -> PersonaError | None:
    """The refusal ``call`` ended in, or ``None`` when it went through."""
    try:
        call()
    except PersonaError as exc:
        return exc
    return None


def _holding_the_secret(root: Path) -> list[str]:
    """Every file under ``root`` whose bytes hold the secret, links followed: what a reader
    of the destination, or a ``git add`` of it, would get out."""
    found: list[str] = []
    for folder, _dirs, files in os.walk(root, followlinks=True):
        for name in files:
            path = Path(folder) / name
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if SECRET in data:
                found.append(path.relative_to(root).as_posix())
    return sorted(found)


def _snapshot(root: Path) -> dict[str, str]:
    """Everything under ``root``, links not followed: a directory, a link and where it
    points, or a file's sha256."""
    seen: dict[str, str] = {}
    for folder, dirs, files in os.walk(root):
        for name in [*dirs, *files]:
            path = Path(folder) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                seen[relative] = f"link -> {os.readlink(path)}"
            elif path.is_dir():
                seen[relative] = "directory"
            else:
                seen[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return seen


def _links(snapshot: dict[str, str]) -> list[str]:
    return sorted(path for path, what in snapshot.items() if what.startswith("link -> "))


def _the_rest(source: Path) -> dict[str, str]:
    """What a copy of ``source`` must hold: every directory and file of it, and no link."""
    return {
        path: what for path, what in _snapshot(source).items() if not what.startswith("link -> ")
    }


def _copied(destination: Path) -> dict[str, str]:
    """What a copy holds, aisquare's own sidecar aside (it is written, never copied)."""
    copied = _snapshot(destination)
    assert copied.pop(".persona.json", None), "the copy has no sidecar"
    return copied


# --- import: every link is left out, and named ----------------------------------------------


@pytest.mark.parametrize("plant", LINKS)
def test_import_copies_everything_but_the_link_and_names_what_it_skipped(
    runner: CliRunner, repo: Path, vault: Path, plant: Plant
) -> None:
    skill = _skill(repo / ".claude" / "skills")
    named = plant(skill, vault)

    result = _import(str(skill), root=repo)  # the service, into the layer that is committed
    by_cli = runner.invoke(app, ["persona", "import", str(skill), "--user"])

    project, user = repo / ".aisquare", cli_suite._user_layer()
    assert result.persona.path == project / "personas" / "helper"
    for layer in (project, user):
        assert _holding_the_secret(layer) == []
        assert _links(_snapshot(layer)) == []
    assert result.skipped_links == [named]
    assert by_cli.exit_code == 0, repr(by_cli.exception)
    warned = f"⚠ skipped 1 symbolic link, never followed and not copied: {named}"
    assert warned in by_cli.stderr.splitlines()
    for copy in (result.persona.path, user / "helper"):
        assert _copied(copy) == _the_rest(skill)  # the rest, byte for byte


def test_every_skipped_link_is_named_once_in_path_order(repo: Path, vault: Path) -> None:
    skill = _skill(repo / ".claude" / "skills")
    _link(skill / "z-link", vault / "id_ed25519")
    _link(skill / "y-link", vault, directory=True)
    _link(skill / "references" / "a-link", vault / "id_ed25519")

    result = _import(str(skill), root=repo)

    assert result.skipped_links == ["references/a-link", "y-link", "z-link"]
    assert service.link_warning(result.skipped_links) == (
        "skipped 3 symbolic links, never followed and not copied: references/a-link, y-link, z-link"
    )


def test_a_linked_sidecar_is_not_read_either(repo: Path, vault: Path) -> None:
    """Import reads a source's ``.persona.json`` (a kept draft being finished carries its
    engine): a link in its place is skipped like any other, and never read."""
    skill = _skill(repo / ".claude" / "skills")
    forged = vault / "sidecar.json"
    forged.write_text(
        Provenance(
            source="stdin",
            source_sha256="0" * 64,
            engine="manager",
            model="not-this-skills",
            imported_at=datetime(2026, 1, 1, tzinfo=UTC),
        ).model_dump_json(),
        encoding="utf-8",
    )
    _link(skill / ".persona.json", forged)

    result = _import(str(skill), root=repo)

    sidecar = json.loads((result.persona.path / ".persona.json").read_text(encoding="utf-8"))
    assert (sidecar["engine"], sidecar["model"]) == ("copy", None)
    assert result.skipped_links == [".persona.json"]


def test_persona_import_of_the_reviews_helper_warns_in_one_line_and_json_lists_the_links(
    runner: CliRunner, repo: Path, vault: Path
) -> None:
    skill = _skill(repo / ".claude" / "skills")
    _to_a_file(skill, vault)
    _relative_and_out_of_the_repository(skill, vault)

    listed = runner.invoke(app, ["--json", "persona", "import", "--list"])
    human = runner.invoke(app, ["persona", "import", "helper", "--project"])
    machine = runner.invoke(app, ["--json", "persona", "import", "helper", "--project", "--force"])

    layer = repo / ".aisquare"
    assert _holding_the_secret(layer) == []
    assert _links(_snapshot(layer)) == []
    assert listed.exit_code == 0, repr(listed.exception)
    assert [found["name"] for found in json.loads(listed.stdout)["skills"]] == ["helper"]
    assert human.exit_code == 0, repr(human.exception)
    assert human.stdout.startswith("✓ imported helper (copy) into project: ")
    assert [line for line in human.stderr.splitlines() if "symbolic link" in line] == [
        "⚠ skipped 2 symbolic links, never followed and not copied: env.txt, refs"
    ]
    assert machine.exit_code == 0, repr(machine.exception)
    assert json.loads(machine.stdout)["skipped_links"] == ["env.txt", "refs"]
    assert _copied(layer / "personas" / "helper") == _the_rest(skill)


# --- export: the same ---------------------------------------------------------------------


@pytest.mark.parametrize("plant", LINKS)
def test_export_copies_everything_but_the_link_and_names_what_it_skipped(
    runner: CliRunner, tmp_path: Path, repo: Path, vault: Path, plant: Plant
) -> None:
    persona = _skill(repo / ".aisquare" / "personas")  # as a cloned repository ships one
    named = plant(persona, vault)
    out, other = tmp_path / "out", tmp_path / "by-cli"

    result = _export(repo, out)
    by_cli = runner.invoke(app, ["persona", "export", "helper", "--to", str(other)])

    for destination in (out, other):
        assert _holding_the_secret(destination) == []
        assert _links(_snapshot(destination)) == []
    assert isinstance(result, service.ExportResult)
    assert result.path == out / "helper"
    assert result.skipped_links == [named]
    assert by_cli.exit_code == 0, repr(by_cli.exception)
    warned = f"⚠ skipped 1 symbolic link, never followed and not copied: {named}"
    assert warned in by_cli.stderr.splitlines()
    for destination in (out, other):
        assert _copied(destination / "helper") == _the_rest(persona)


def test_persona_export_warns_in_one_line_whatever_the_destination_and_json_lists_the_links(
    runner: CliRunner, tmp_path: Path, repo: Path, vault: Path, claude_dir: Path
) -> None:
    persona = _skill(repo / ".aisquare" / "personas")
    _to_a_file(persona, vault)
    out = tmp_path / "out"

    to_a_directory = runner.invoke(app, ["persona", "export", "helper", "--to", str(out)])
    to_claude = runner.invoke(app, ["persona", "export", "helper", "--skill", "--user"])
    to_the_project = runner.invoke(
        app, ["--json", "persona", "export", "helper", "--skill", "--project"]
    )

    written = (out, claude_dir / "skills", repo / ".claude" / "skills")
    for destination in written:
        assert _holding_the_secret(destination) == []
        assert _links(_snapshot(destination)) == []
    for result in (to_a_directory, to_claude):
        assert result.exit_code == 0, repr(result.exception)
        assert result.stdout.startswith("✓ exported helper to ")
        assert [line for line in result.stderr.splitlines() if "symbolic link" in line] == [
            "⚠ skipped 1 symbolic link, never followed and not copied: env.txt"
        ]
    assert to_the_project.exit_code == 0, repr(to_the_project.exception)
    assert json.loads(to_the_project.stdout)["skipped_links"] == ["env.txt"]
    for destination in written:
        assert _copied(destination / "helper") == _the_rest(persona)


# --- the one link that cannot be skipped: SKILL.md itself -----------------------------------


def test_import_still_refuses_a_skill_md_that_is_itself_a_link_and_writes_nothing(
    tmp_path: Path, repo: Path, vault: Path
) -> None:
    skill = _skill(repo / ".claude" / "skills")
    _in_place_of_the_skill_md(skill, vault)
    before = _snapshot(tmp_path)

    refusal = _refused(lambda: _import(str(skill), root=repo))

    assert refusal is not None, "the import went through"
    assert refusal.code == CODE
    assert str(refusal).startswith(f"{skill}: 'SKILL.md' is a symbolic link")
    assert REMEDY in str(refusal)
    assert _snapshot(tmp_path) == before  # nothing was written, anywhere


def test_export_still_refuses_a_skill_md_that_is_itself_a_link_and_writes_nothing(
    tmp_path: Path, repo: Path, vault: Path
) -> None:
    persona = _skill(repo / ".aisquare" / "personas")
    _in_place_of_the_skill_md(persona, vault)
    before = _snapshot(tmp_path)

    refusal = _refused(lambda: _export(repo, tmp_path / "out"))

    assert refusal is not None, "the export went through"
    assert refusal.code == CODE
    assert str(refusal).startswith(f"{persona}: 'SKILL.md' is a symbolic link")
    assert REMEDY in str(refusal)
    assert _snapshot(tmp_path) == before  # not even the destination's parent was made


def test_a_forced_import_that_is_refused_leaves_the_persona_it_would_replace_untouched(
    repo: Path, vault: Path
) -> None:
    skill = _skill(repo / ".claude" / "skills")
    dest = _import(str(skill), root=repo).persona.path  # imported while its SKILL.md was real
    before = _snapshot(dest)
    _in_place_of_the_skill_md(skill, vault)

    refusal = _refused(lambda: _import(str(skill), root=repo, force=True))

    assert refusal is not None, "the import went through"
    assert refusal.code == CODE
    assert _snapshot(dest) == before
    assert sorted(path.name for path in dest.parent.iterdir()) == ["helper"]  # no staging left


def _recording_engines(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Both import engines replaced by one that records the text it is handed, then is
    unavailable (as conftest's ``no_real_llm_import`` leaves them)."""
    handed: list[str] = []

    def engine(source_text: str, **_kwargs: object) -> NoReturn:
        handed.append(source_text)
        raise persona_import.EngineUnavailable("tests never run a model")

    monkeypatch.setattr(persona_import, "draft_with_manager", engine)
    monkeypatch.setattr(persona_import, "draft_with_api", engine)
    return handed


def test_a_skill_md_that_is_a_link_is_never_read_out_to_an_import_engine(
    tmp_path: Path, repo: Path, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The target is not a skill, so the import would take the LLM path with its text."""
    skill = _skill(repo / ".claude" / "skills")
    (skill / "SKILL.md").unlink()
    _link(skill / "SKILL.md", vault / "id_ed25519")
    handed = _recording_engines(monkeypatch)
    before = _snapshot(tmp_path)

    refusal = _refused(lambda: _import(str(skill), root=repo))

    assert handed == []  # the secret never reached an engine
    assert refusal is not None and refusal.code == CODE
    assert f"{skill}: 'SKILL.md' is a symbolic link" in str(refusal)
    assert _snapshot(tmp_path) == before  # and no draft was kept of it


@pytest.mark.parametrize(
    ("llm", "condense"), [("always", False), ("auto", True)], ids=["--llm", "--condense"]
)
def test_a_link_beside_the_skill_md_does_not_stop_the_llm_path_which_reads_the_skill_md_alone(
    repo: Path,
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    llm: Literal["auto", "always"],
    condense: bool,
) -> None:
    skill = _skill(repo / ".claude" / "skills")
    _to_a_directory(skill, vault)
    handed = _recording_engines(monkeypatch)

    refusal = _refused(lambda: _import(str(skill), root=repo, llm=llm, condense=condense))

    text = (skill / "SKILL.md").read_bytes().decode("utf-8")
    assert handed == [text, text]  # each engine was asked, with the real SKILL.md and no more
    assert refusal is not None and refusal.code == "no_import_engine"


# --- a tree without a link is copied as before, and reports nothing --------------------------


def test_a_tree_without_a_link_is_copied_whole_and_reports_nothing_skipped(
    runner: CliRunner, tmp_path: Path, repo: Path
) -> None:
    skill = _skill(repo / ".claude" / "skills")

    imported = _import(str(skill), root=repo)
    exported = _export(repo, tmp_path / "out")
    by_cli = runner.invoke(app, ["persona", "import", str(skill), "--user"])
    as_json = runner.invoke(
        app, ["--json", "persona", "export", "helper", "--to", str(tmp_path / "again")]
    )

    assert isinstance(exported, service.ExportResult)
    assert imported.skipped_links == [] and exported.skipped_links == []
    assert service.link_warning([]) is None
    assert cli_suite._tree(imported.persona.path) == cli_suite._tree(skill)
    assert cli_suite._tree(exported.path) == cli_suite._tree(skill)
    assert set(cli_suite._tree(skill)) == {"SKILL.md", "references/guide.md"}
    assert by_cli.exit_code == 0, repr(by_cli.exception)
    assert "symbolic link" not in by_cli.stderr
    assert as_json.exit_code == 0, repr(as_json.exception)
    assert json.loads(as_json.stdout)["skipped_links"] == []


def test_a_skill_reached_through_a_link_is_not_a_skill_that_holds_one(
    tmp_path: Path, claude_dir: Path
) -> None:
    """The dotfiles arrangement keeps working: only a link INSIDE the skill directory is
    left out. Skills kept in a dotfiles checkout, with Claude Code's ``skills`` directory a
    link to it, are still offered and still copied byte for byte, with nothing skipped."""
    dotfiles = tmp_path / "dotfiles" / "skills"
    real = cli_suite._skill(dotfiles, "notes", files={"references/guide.md": b"guide\n"})
    claude_dir.mkdir()
    _link(claude_dir / "skills", dotfiles, directory=True)

    imported = _import("notes", root=None, layer="user")

    assert cli_suite._tree(imported.persona.path) == cli_suite._tree(real)
    assert imported.skipped_links == []


# --- the dialogs show the sentence the CLI prints ---------------------------------------------

TWO_LINKS = "skipped 2 symbolic links, never followed and not copied: env.txt, refs"


def test_the_import_dialog_shows_the_warning_whoever_opened_it(
    skills: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dialog toasts it itself: the Spawn dialog opens it too, and shows no warnings."""
    directory = ui_suite.write_persona(
        ui_suite.user_layer(), "helper", ui_suite.skill_text("Helps.")
    )

    def answer(source: str, **_: object) -> service.ImportResult:
        return service.ImportResult(
            persona=ui_suite.persona(directory),
            engine="copy",
            source="test",
            skipped_links=["env.txt", "refs"],
        )

    monkeypatch.setattr(service, "import_source", Recorder(answer))
    dialog = ImportPersonaScreen(None)

    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        dialog.query_one("#import-source", Input).value = "./skills/helper"
        await pilot.pause()
        await press(pilot, "#import-submit")
        await settle(pilot)
        return [type(host.screen).__name__, list(host.notices), list(host.results)]

    screen, notices, results = drive(scenario, dialog=dialog)
    assert screen == "Screen"  # dismissed with the result, as before
    assert (TWO_LINKS, "warning") in notices
    assert [result.skipped_links for result in results] == [["env.txt", "refs"]]


def test_the_export_dialog_shows_the_warning(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written = Path("/written/pair")
    export = Recorder(
        lambda name, **_: service.ExportResult(path=written, skipped_links=["env.txt", "refs"])
    )
    monkeypatch.setattr(service, "export", export)
    dialog = ExportPersonaScreen("pair", repo)

    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        dialog.query_one("#export-directory", RadioButton).value = True
        await pilot.pause()
        dialog.query_one("#export-dir", Input).value = "./persona-copies"
        await pilot.pause()
        await press(pilot, "#export-submit")
        return [list(host.notices), list(host.results)]

    notices, results = drive(scenario, dialog=dialog)
    assert len(export.calls) == 1
    assert (TWO_LINKS, "warning") in notices
    assert results == [ExportDone("pair", written, None)]


def test_save_as_shows_the_warning(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    export = Recorder(
        lambda name, *, root, to, skill, force: service.ExportResult(
            path=to / name, skipped_links=["env.txt", "refs"]
        )
    )
    monkeypatch.setattr(service, "export", export)
    dialog = EditPersonaScreen("skeptic", "bundled", core.BUNDLED_DIR / "skeptic", repo)

    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        await press(pilot, "#edit-save-as")
        await settle(pilot)
        return [list(host.notices), list(host.results)]

    notices, results = drive(scenario, dialog=dialog)
    assert len(export.calls) == 1
    assert (TWO_LINKS, "warning") in notices
    assert results == [True]
