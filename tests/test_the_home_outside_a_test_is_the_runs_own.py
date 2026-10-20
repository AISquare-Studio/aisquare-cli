"""Wherever no test's own home is in force, the home is the run's, never ``~/.aisquare``.

What a test starts can outlive its teardown: the needs watcher's scan runs on past its
lifespan's join, and the R panel's writer thread runs the writes still queued on it. A
module-scoped fixture runs before ``isolated_home`` too. With no home named between two
tests, all of them read and wrote the developer's own ``~/.aisquare``; on Windows that is
``%USERPROFILE%\\.aisquare``, beside ``%TEMP%``, and one file of theirs there made the
user's home the project root of every markerless directory of every later test: 49
failures and errors on the windows-latest leg of #243 (``conftest.home_outside_a_test``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aisquare.core import paths
from aisquare.core.workspace import ROOT_MARKERS
from tests import conftest


@pytest.fixture(scope="module")
def home_between_tests() -> Path:
    """``aisquare_home()`` where no test's own home is in force.

    MODULE scope is the point: pytest sets this up before the function-scoped autouse
    ``isolated_home``, which is when a thread a test left running reads the home too.
    """
    return paths.aisquare_home()


def test_between_tests_the_home_is_the_runs_own_or_the_one_the_shell_named(
    home_between_tests: Path, home_outside_a_test: Path, isolated_home: Path
) -> None:
    assert paths.aisquare_home() == isolated_home, "the control: in a test, the test's own"
    assert home_between_tests != isolated_home
    assert home_between_tests == home_outside_a_test
    if conftest.SHELL_HOME is not None:
        return  # the CI ambient job's configured home, which it is there to keep
    assert home_between_tests != Path.home() / ".aisquare", (
        "between two tests the home is the developer's own: what a test left running "
        "writes into it, and a module-scoped fixture reads it"
    )


def test_the_home_between_tests_is_no_project_marker_above_a_tests_directory(
    home_between_tests: Path, tmp_path: Path
) -> None:
    """Anything written there must not make a test's directory part of another project: a
    home named like a marker in a directory above ``tmp_path`` would, as ``~/.aisquare``
    did on Windows."""
    marker_like = home_between_tests.name in ROOT_MARKERS
    above = tmp_path.resolve().is_relative_to(home_between_tests.parent.resolve())
    assert not (marker_like and above), home_between_tests
    assert set(conftest._ROOT_MARKERS) == set(ROOT_MARKERS), "the summary watches every marker"
