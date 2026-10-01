"""``persona import`` and ``persona export`` refuse a skill directory that holds a symbolic link.

Review of #240, finding 15. Both copied with ``shutil.copytree``'s default, which follows
every link in the tree and writes what it points at as a regular file. A cloned
repository's ``.claude/skills/helper`` holding ``env.txt -> /proc/self/environ`` or
``refs -> ../../../../.ssh`` was offered by ``persona import --list``, and ``persona import
helper --project`` wrote the process's environment, or the owner's keys, into
``<repo>/.aisquare/personas/helper``: the layer that is committed. ``persona export`` wrote
them out again.

The ruling: refuse. A link anywhere in the tree, to a file or to a directory, wherever it
points, stops the import or the export with a ``PersonaError`` that names the first one
found, and nothing is written. ``import --list`` may still offer the skill.

The links are real ones, made with ``os.symlink`` under ``tmp_path``; where this machine
cannot make one (Windows without the privilege) the test skips. The secret is a fake.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Literal, NoReturn

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.personas import Layer, PersonaError
from aisquare.services import persona_import
from aisquare.services import personas as service
from tests import test_persona_cli as cli_suite
from tests.fsperms import can_symlink

# The CLI suite's fixtures, bound here so pytest finds them for this module's tests.
claude_dir = cli_suite.claude_dir
repo = cli_suite.repo

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
    pytest.param(_in_place_of_the_skill_md, id="in-place-of-the-skill-md"),
]


def _never_asked(_draft: service.PersonaDraftView) -> bool:
    raise AssertionError("a refused import never reaches a confirmation")


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


# --- import -------------------------------------------------------------------------------


@pytest.mark.parametrize("plant", LINKS)
def test_import_refuses_a_skill_that_holds_a_link_and_writes_nothing(
    tmp_path: Path, repo: Path, vault: Path, plant: Plant
) -> None:
    skill = _skill(repo / ".claude" / "skills")
    named = plant(skill, vault)
    before = _snapshot(tmp_path)

    refusal = _refused(lambda: _import(str(skill), root=repo))

    assert _holding_the_secret(repo / ".aisquare") == []  # the layer that is committed
    assert refusal is not None, "the import went through"
    assert refusal.code == CODE
    assert str(refusal).startswith(f"{skill}: '{named}' is a symbolic link")
    assert REMEDY in str(refusal)
    assert _snapshot(tmp_path) == before  # nothing was written, anywhere


def test_the_refusal_names_the_first_link_the_shallowest_then_by_name(
    repo: Path, vault: Path
) -> None:
    skill = _skill(repo / ".claude" / "skills")
    _link(skill / "references" / "a-link", vault / "id_ed25519")  # first by name, but deeper
    _link(skill / "z-link", vault / "id_ed25519")
    _link(skill / "y-link", vault, directory=True)

    refusal = _refused(lambda: _import(str(skill), root=repo))

    assert refusal is not None, "the import went through"
    assert f"{skill}: 'y-link' is a symbolic link" in str(refusal)


def test_a_forced_import_that_is_refused_leaves_the_persona_it_would_replace_untouched(
    repo: Path, vault: Path
) -> None:
    skill = _skill(repo / ".claude" / "skills")
    dest = _import(str(skill), root=repo).persona.path  # imported while it held no link
    before = _snapshot(dest)
    _link(skill / "env.txt", vault / "id_ed25519")

    refusal = _refused(lambda: _import(str(skill), root=repo, force=True))

    assert _holding_the_secret(repo / ".aisquare") == []
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
def test_the_llm_path_refuses_the_same_tree_before_an_engine_is_asked(
    tmp_path: Path,
    repo: Path,
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    llm: Literal["auto", "always"],
    condense: bool,
) -> None:
    skill = _skill(repo / ".claude" / "skills")
    _link(skill / "refs", vault, directory=True)
    handed = _recording_engines(monkeypatch)
    before = _snapshot(tmp_path)

    refusal = _refused(lambda: _import(str(skill), root=repo, llm=llm, condense=condense))

    assert handed == []
    assert refusal is not None and refusal.code == CODE
    assert _snapshot(tmp_path) == before


def test_persona_import_of_the_reviews_helper_exits_1_and_import_list_still_offers_it(
    runner: CliRunner, repo: Path, vault: Path
) -> None:
    skill = _skill(repo / ".claude" / "skills")
    _to_a_file(skill, vault)
    _relative_and_out_of_the_repository(skill, vault)

    listed = runner.invoke(app, ["--json", "persona", "import", "--list"])
    human = runner.invoke(app, ["persona", "import", "helper", "--project"])
    machine = runner.invoke(app, ["--json", "persona", "import", "helper", "--project"])

    assert _holding_the_secret(repo / ".aisquare") == []
    assert listed.exit_code == 0, repr(listed.exception)
    assert [found["name"] for found in json.loads(listed.stdout)["skills"]] == ["helper"]
    assert isinstance(human.exception, SystemExit) and human.exit_code == 1
    assert f"{skill}: 'env.txt' is a symbolic link" in human.stderr
    assert REMEDY in human.stderr
    payload = json.loads(machine.stdout)
    assert (machine.exit_code, payload["error"]) == (1, CODE)
    assert "'env.txt' is a symbolic link" in payload["detail"]
    assert not (repo / ".aisquare").exists()


# --- export -------------------------------------------------------------------------------


@pytest.mark.parametrize("plant", LINKS)
def test_export_refuses_a_persona_that_holds_a_link_and_writes_nothing(
    tmp_path: Path, repo: Path, vault: Path, plant: Plant
) -> None:
    persona = _skill(repo / ".aisquare" / "personas")  # as a cloned repository ships one
    named = plant(persona, vault)
    out = tmp_path / "out"
    before = _snapshot(tmp_path)

    refusal = _refused(lambda: service.export("helper", root=repo, to=out, skill=None, force=False))

    assert _holding_the_secret(out) == []
    assert refusal is not None, "the export went through"
    assert refusal.code == CODE
    assert str(refusal).startswith(f"{persona}: '{named}' is a symbolic link")
    assert REMEDY in str(refusal)
    assert _snapshot(tmp_path) == before  # not even the destination's parent was made


def test_persona_export_exits_1_naming_the_link_whatever_the_destination(
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

    for destination in (out, claude_dir, repo / ".claude"):
        assert _holding_the_secret(destination) == []
    for result in (to_a_directory, to_claude):
        assert isinstance(result.exception, SystemExit) and result.exit_code == 1
        assert f"{persona}: 'env.txt' is a symbolic link" in result.stderr
        assert REMEDY in result.stderr
    payload = json.loads(to_the_project.stdout)
    assert (to_the_project.exit_code, payload["error"]) == (1, CODE)
    for destination in (out, claude_dir, repo / ".claude"):
        assert not destination.exists()


# --- a tree without a link is copied as before ------------------------------------------------


def test_replacing_the_link_with_a_real_file_is_all_it_takes(
    tmp_path: Path, repo: Path, vault: Path
) -> None:
    skill = _skill(repo / ".claude" / "skills")
    _to_a_file(skill, vault)
    assert _refused(lambda: _import(str(skill), root=repo)) is not None

    (skill / "env.txt").unlink()
    (skill / "env.txt").write_bytes(b"NAME=value\n")
    imported = _import(str(skill), root=repo).persona.path
    exported = service.export("helper", root=repo, to=tmp_path / "out", skill=None, force=False)

    assert cli_suite._tree(imported) == cli_suite._tree(skill)
    assert isinstance(exported, Path)
    assert cli_suite._tree(exported) == cli_suite._tree(skill)
    assert set(cli_suite._tree(skill)) == {"SKILL.md", "env.txt", "references/guide.md"}


def test_a_skill_reached_through_a_link_is_not_a_skill_that_holds_one(
    tmp_path: Path, claude_dir: Path
) -> None:
    """A control, green before the fix too: the refusal is for a link INSIDE the skill
    directory. Skills kept in a dotfiles checkout, with Claude Code's ``skills`` directory
    a link to it, are still offered and still copied byte for byte."""
    dotfiles = tmp_path / "dotfiles" / "skills"
    real = cli_suite._skill(dotfiles, "notes", files={"references/guide.md": b"guide\n"})
    claude_dir.mkdir()
    _link(claude_dir / "skills", dotfiles, directory=True)

    imported = _import("notes", root=None, layer="user").persona.path

    assert cli_suite._tree(imported) == cli_suite._tree(real)
