"""The release's two builds (#137): rlx, and the same build as aisquare-cli.

``scripts/build_dists.py`` builds both and refuses to finish unless they are one package
under two names; publish.yml uploads rlx first, then aisquare-cli; installers.yml waits for
both; ci.yml builds and checks them on every pull request, and nothing uploads anywhere but
publish.yml on a published release.
"""

from __future__ import annotations

import importlib.util
import sys
import tomllib
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "build_dists", ROOT / "scripts" / "build_dists.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_dists"] = module
    spec.loader.exec_module(module)
    return module


build_dists = _script()
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def _wheel(
    folder: Path,
    name: str,
    *,
    version: str = VERSION,
    files: dict[str, str] | None = None,
    summary: str = "rlx",
    scripts: str = "[console_scripts]\nrlx = aisquare.cli.app:main\n",
) -> Path:
    """A wheel of ``name`` holding ``files`` (``aisquare/__init__.py`` by default)."""
    folder.mkdir(parents=True, exist_ok=True)
    normal = name.replace("-", "_")
    wheel = folder / f"{normal}-{version}-py3-none-any.whl"
    info = f"{normal}-{version}.dist-info"
    with zipfile.ZipFile(wheel, "w") as archive:
        for member, text in (files or {"aisquare/__init__.py": "x = 1\n"}).items():
            archive.writestr(member, text)
        archive.writestr(
            f"{info}/METADATA",
            f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\nSummary: {summary}\n"
            "\nReadme\n",
        )
        archive.writestr(f"{info}/entry_points.txt", scripts)
        archive.writestr(f"{info}/RECORD", "")
    (folder / f"{normal}-{version}.tar.gz").write_bytes(b"sdist")
    return wheel


# --- one package under two names ------------------------------------------------------------


def test_the_project_is_named_rlx_and_the_alias_differs_by_that_line_alone() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    renamed = build_dists.renamed(pyproject, "aisquare-cli")

    assert tomllib.loads(pyproject)["project"]["name"] == "rlx"
    assert tomllib.loads(renamed)["project"]["name"] == "aisquare-cli"
    changed = [
        (old, new)
        for old, new in zip(pyproject.splitlines(), renamed.splitlines(), strict=True)
        if old != new
    ]
    assert changed == [('name = "rlx"', 'name = "aisquare-cli"')]


def test_a_name_line_in_another_table_is_left_alone() -> None:
    text = '[project]\nname = "rlx"\nversion = "1"\n\n[tool.x]\nname = "rlx"\n'
    assert build_dists.renamed(text, "aisquare-cli") == (
        '[project]\nname = "aisquare-cli"\nversion = "1"\n\n[tool.x]\nname = "rlx"\n'
    )


def test_a_project_without_one_name_line_is_refused() -> None:
    with pytest.raises(SystemExit, match="exactly one name line, found 0"):
        build_dists.renamed('[project]\nversion = "1"\n', "aisquare-cli")


def test_two_builds_of_one_package_compare_clean(tmp_path: Path) -> None:
    first = _wheel(tmp_path / "rlx", "rlx")
    second = _wheel(tmp_path / "aisquare-cli", "aisquare-cli")

    assert build_dists.compare(first, second) == []
    assert build_dists.check(tmp_path, f"v{VERSION}") == []


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"files": {"aisquare/__init__.py": "x = 2\n"}}, "aisquare/__init__.py differs"),
        (
            {"files": {"aisquare/__init__.py": "x = 1\n", "aisquare/extra.py": ""}},
            "aisquare/extra.py is in only one of the wheels",
        ),
        ({"summary": "another"}, "their METADATA differs in more than the Name"),
        ({"scripts": "[console_scripts]\n"}, "their entry points differ"),
    ],
    ids=["a-file-differs", "a-file-more", "metadata", "entry-points"],
)
def test_two_builds_that_differ_are_refused_naming_what(
    tmp_path: Path, change: dict[str, Any], problem: str
) -> None:
    first = _wheel(tmp_path / "rlx", "rlx")
    second = _wheel(tmp_path / "aisquare-cli", "aisquare-cli", **change)

    found = build_dists.compare(first, second)

    assert any(problem in line for line in found), found


def test_a_version_that_is_not_pyproject_s_or_the_tag_s_is_refused(tmp_path: Path) -> None:
    _wheel(tmp_path / "rlx", "rlx")
    _wheel(tmp_path / "aisquare-cli", "aisquare-cli", version="9.9.9")

    found = build_dists.check(tmp_path, "v1.2.3")

    assert f"aisquare-cli 9.9.9 is not pyproject's {VERSION}" in found, found
    assert f"rlx {VERSION} is not the tag's v1.2.3" in found, found


def test_a_folder_with_another_name_s_build_in_it_is_refused(tmp_path: Path) -> None:
    _wheel(tmp_path / "rlx", "rlx")
    _wheel(tmp_path / "rlx", "aisquare-cli")
    _wheel(tmp_path / "aisquare-cli", "aisquare-cli")

    with pytest.raises(SystemExit, match=r"must hold one rlx \.whl and nothing else"):
        build_dists.check(tmp_path, None)


# --- the workflows --------------------------------------------------------------------------


def _workflow(name: str) -> dict[str, Any]:
    loaded: dict[Any, Any] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    loaded["on"] = loaded.pop(True, loaded.get("on"))
    return loaded


def test_publish_builds_both_names_and_uploads_rlx_first_then_aisquare_cli() -> None:
    jobs = _workflow("publish.yml")["jobs"]
    built = "\n".join(step.get("run", "") for step in jobs["build"]["steps"])
    build_step = next(
        step for step in jobs["build"]["steps"] if "build_dists" in step.get("run", "")
    )
    uploads = [
        step
        for step in jobs["publish"]["steps"]
        if "pypa/gh-action-pypi-publish" in step.get("uses", "")
    ]

    assert 'python scripts/build_dists.py --out dist --tag "$TAG"' in built
    assert build_step["env"]["TAG"] == "${{ github.event.release.tag_name }}"
    assert [step["with"]["packages-dir"] for step in uploads] == ["dist/rlx/", "dist/aisquare-cli/"]
    assert all(step["with"]["skip-existing"] is True for step in uploads)
    assert all("repository-url" not in step["with"] for step in uploads), "PyPI, never TestPyPI"
    assert jobs["publish"]["permissions"] == {"id-token": "write"}
    assert "environment" not in jobs["publish"], "the trusted publishers name no environment"


def test_the_installers_wait_for_both_names_on_pypi() -> None:
    steps = _workflow("installers.yml")["jobs"]["installers"]["steps"]
    wait = next(step for step in steps if step.get("name") == "Wait until PyPI serves the version")

    assert "for name in rlx aisquare-cli; do" in wait["run"]
    assert '"$name==$version"' in wait["run"]


def test_ci_builds_and_checks_both_names_and_uploads_nothing() -> None:
    ci = _workflow("ci.yml")
    steps = ci["jobs"]["package"]["steps"]
    runs = "\n".join(step.get("run", "") for step in steps)

    assert "python scripts/build_dists.py --out dist" in runs
    assert "twine check --strict dist/rlx/* dist/aisquare-cli/*" in runs
    assert 'assert names == ["aisquare-cli", "rlx"]' in runs
    for name, workflow in (("ci.yml", ci), ("installers.yml", _workflow("installers.yml"))):
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                assert "pypi-publish" not in step.get("uses", ""), f"{name} uploads"
                assert "twine upload" not in step.get("run", ""), f"{name} uploads"
