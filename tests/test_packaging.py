"""Both console scripts point at the same CLI entry point."""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path

import pytest

import aisquare


def test_console_scripts_registered() -> None:
    scripts = {ep.name: ep.value for ep in entry_points(group="console_scripts")}
    assert scripts.get("aisquare") == "aisquare.cli.app:main"
    assert scripts.get("asq") == "aisquare.cli.app:main"


def test_the_suite_imports_this_tree_not_an_installed_copy() -> None:
    """The gate must validate this checkout, not whatever PATH's pytest imports.

    With the src layout, a pytest from a sibling interpreter resolves
    ``aisquare`` from that env's site-packages — the suite then judges a stale
    snapshot while reporting on the tree. That failure mode is silent: tests
    for code that snapshot already has pass, tests for anything newer fail as
    if the tree were broken. If this assert trips, your pytest is not this
    project's: run `make check` (venv-pinned) or activate `.venv`.
    """
    src = Path(__file__).resolve().parents[1] / "src"
    module_file = Path(aisquare.__file__).resolve()
    assert module_file.is_relative_to(src), (
        f"aisquare imported from {module_file}, not from {src} — "
        "this run is validating an installed copy, not this tree"
    )


def test_the_base_install_gains_no_dependencies() -> None:
    """The experiment must not change what a normal `pip install aisquare-cli`
    pulls in. Pinned as a set, so adding one is a deliberate act with a
    conversation attached rather than a line that slipped through review.

    ``textual`` is in the set because the fleet UI made it core in 0.6.0, not
    because the experiment wants it: the CI transport is stdlib ``urllib`` so
    that the hook path works in a base install.

    ``pyyaml`` is in the set because a persona is a Claude Code skill directory
    and a skill's frontmatter is full YAML (docs/plans/spawn-personas.md §3.3,
    accepted by the owner for interchange fidelity); it is imported inside the
    parser only."""
    import re
    import tomllib

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    # Split on every specifier character, so a future `foo<2` upper bound reads
    # as `foo` rather than failing with a confusing diff.
    required = {re.split(r"[<>=!~\[; ]", dep)[0].strip() for dep in data["project"]["dependencies"]}
    assert required == {"typer", "rich", "pydantic", "tomli-w", "textual", "pyyaml"}


def test_the_experiment_extra_is_a_real_extra_in_the_built_metadata() -> None:
    """`pip install 'aisquare-cli[experiment]'` has to be a real command even
    while the extra adds nothing — the transport is stdlib on purpose. Asserted
    on the BUILT metadata rather than on the pyproject table, so a build backend
    that dropped empty extras would fail this, and so the extra may later take
    the OpenTelemetry dependency without failing it.

    ``importlib.metadata`` reads whatever distribution is INSTALLED, which is
    only this tree's metadata when the two versions agree. In a worktree whose
    venv points at a sibling checkout it is a different package altogether, and
    asserting on it fails for the environment rather than for the build — so the
    versions are compared first and the check says which it is. CI installs this
    tree, and its `package (build + install)` job builds the real wheel, so this
    is a genuine assertion there and never a silent pass."""
    import tomllib
    from importlib.metadata import metadata, version

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    installed = version("aisquare-cli")
    if installed != declared:
        pytest.skip(
            f"the installed aisquare-cli is {installed}, this tree is {declared} — "
            "its metadata is another build's; reinstall with `make install` to check it here"
        )
    assert "experiment" in (metadata("aisquare-cli").get_all("Provides-Extra") or [])


def test_the_wheel_carries_the_bundled_personas(tmp_path: Path) -> None:
    """The bundled personas are data files, not modules. An editable install
    reads them from the tree whether or not a wheel would ship them, so only a
    real build proves `pip install aisquare-cli` gets `persona list`'s four.
    Built with hatchling — the project's own backend — from THIS tree."""
    import zipfile

    from hatchling.builders.wheel import WheelBuilder

    root = Path(__file__).resolve().parents[1]
    wheels = list(WheelBuilder(str(root)).build(directory=str(tmp_path), versions=["standard"]))

    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
    for persona in ("careful", "mentor", "minimalist", "skeptic"):
        assert f"aisquare/personas/{persona}/SKILL.md" in names
