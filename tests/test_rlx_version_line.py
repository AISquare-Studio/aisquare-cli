"""``--version`` prints ``rlx <version>`` (#135, RLX R3; the manager's Q7, brief 180), and
every reader of that line still finds the version in it.

The readers, checked when the line changed:
- install.sh's ``version_of_aisquare``: ``aisquare --version | first_line | cut -d' ' -f2``,
  the same function, word for word, in the installers of 0.9.0, 0.9.1, 0.9.2 and 0.10.0, so a
  release's installer run over a newer install reads this line too;
- ``agents.version_in`` (the doctor's hook-binary check, ``aisquare upgrade``'s check of the
  new install): the first dotted version in the output.
Nothing else parses it: CI's wheel smoke and install cell run it and read only its exit.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare import __version__
from aisquare.cli.app import app
from aisquare.core import agents as agent_core

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="install.sh is POSIX sh")


def _version_line() -> str:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    return result.stdout


def test_version_prints_the_products_name_then_the_version() -> None:
    assert _version_line() == f"rlx {__version__}\n"


@POSIX_ONLY
def test_install_shs_reader_finds_the_version_in_it(tmp_path: Path) -> None:
    """A fake ``aisquare`` that prints this release's line, read the way every installer since
    0.9.0 reads it."""
    # Imported here: that module skips itself on Windows, and imported at the top it took
    # this whole file with it, the two tests that run everywhere included.
    from tests.test_install_script_functions import base_path, sh

    line = _version_line()
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    fake = stubs / "aisquare"
    fake.write_text(f"#!/bin/sh\nprintf '%s' '{line}'\n", encoding="utf-8")
    fake.chmod(0o755)

    read = sh("version_of_aisquare", path=f"{stubs}:{base_path(tmp_path)}")

    assert read.returncode == 0, read.stderr
    assert read.stdout.strip() == __version__, (line, read.stdout)


def test_the_python_reader_finds_the_version_in_it() -> None:
    assert agent_core.version_in(_version_line()) == __version__
