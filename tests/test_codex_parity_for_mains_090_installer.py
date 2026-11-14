"""Codex parity for main's 0.9.0 installer hand-off (SCOPE #113 090, #402).

``--agent codex`` goes to the release's own installer only once that one has the option, and
otherwise this script carries on with its own Codex route. These drive ``install.sh``, a POSIX
shell script, so the module is skipped whole on Windows, by its own call (the sweep in
``test_every_test_can_fail.py`` reads it from the module, not from what a module it imports
does): the rest of the family, in ``test_codex_parity_for_mains_090.py``, runs there.
"""

from __future__ import annotations

import sys

import pytest

if sys.platform == "win32":  # pragma: no cover - the POSIX installer's own tests
    pytest.skip("install.sh is a POSIX shell script", allow_module_level=True)

from tests.install_flags import DRY
from tests.test_install_script_handoff import (  # noqa: F401 - fixture, by name
    STAMP,
    Machine,
    machine,
    reporting_release,
)

#: The flags of a run that ends quickly and changes nothing, and asks for Codex: `--no-agent`
#: would contradict `--agent`.
DRY_CODEX = tuple(flag for flag in DRY if flag != "--no-agent")

# --- #402: the installer hand-off ---------------------------------------------------------


def test_a_codex_install_stays_on_this_installer_until_the_release_has_the_option(
    machine: Machine,  # noqa: F811
) -> None:
    machine.pypi(STAMP)
    machine.serve_latest(reporting_release())  # a release that does not parse --agent

    result = machine.run(*DRY_CODEX, "--agent", "codex")

    assert "using this installer (the v7.7.7 installer has no --agent option)" in result.stdout
    assert "RELEASE RAN" not in result.stdout, "the release's installer must not run"
    assert "Codex" in result.stdout, "this script's own Codex route carried on"


def test_a_codex_install_is_handed_to_a_release_that_has_the_option(
    machine: Machine,  # noqa: F811
) -> None:
    machine.pypi(STAMP)
    machine.serve_latest(
        reporting_release(
            options=(
                "--yes",
                "--no-agent",
                "--no-system-deps",
                "--no-project",
                "--project",
                "--version",
                "--version=*",
                "--dry-run",
                "--agent",
            )
        )
    )

    result = machine.run(*DRY_CODEX, "--agent", "codex")

    assert "using the v7.7.7 installer" in result.stdout
    assert "ARG[--agent]" in result.stdout and "ARG[codex]" in result.stdout, result.stdout
