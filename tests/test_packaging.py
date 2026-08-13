"""Both console scripts point at the same CLI entry point."""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path

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
