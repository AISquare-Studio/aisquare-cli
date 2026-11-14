"""Opt-in: the arguments a Codex spawn passes, read by the installed Codex itself.

Run AISQUARE_TEST_CODEX=1 pytest tests/test_codex_native_spawn_args.py. No account, login,
provider credential or model spend is used: Codex reads its command line and prints its
version.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from aisquare.core.config import FleetRoleSettings, RoleLaunchProfile, load_config, save_config
from aisquare.models import ProjectInfo
from aisquare.services import fleet as fleet_service
from tests.test_fleet_service import (  # noqa: F401 - fixtures, by name
    FakeTmux,
    _command,
    project,
    repo,
    tmux,
)


def _codex(binary: str, home: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [binary, *args, "--version"],
        env={**os.environ, "CODEX_HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )


@pytest.mark.skipif(os.environ.get("AISQUARE_TEST_CODEX") != "1", reason="opt-in native Codex test")
def test_real_codex_reads_a_spawn_whose_role_binding_and_own_arguments_name_one_option(
    tmp_path: Path,
    tmux: FakeTmux,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """seq 4204 (1): Codex refuses an option given twice, so a spawn whose role, binding and
    own arguments each named ``--model`` did not start. The spawn's argv now names each once
    (``AgentAdapter.merged_args``), and the installed Codex reads all of it."""
    binary = shutil.which("codex")
    assert binary, "AISQUARE_TEST_CODEX requires an installed Codex"
    home = tmp_path / "codex-home"
    home.mkdir()
    doubled = _codex(binary, home, ["--model", "gpt-role", "-m", "gpt-own"])
    assert doubled.returncode != 0 and "cannot be used multiple times" in doubled.stderr

    config = load_config()
    config.fleet.roles["coder"] = FleetRoleSettings(
        worktree=False,
        agent_args={"codex": ["--model", "gpt-role", "-c", 'model_reasoning_effort="medium"']},
    )
    config.team.profiles["coder"] = RoleLaunchProfile(agent="codex", args=["-m", "gpt-bound"])
    save_config(config)
    fleet_service.spawn(
        project,
        "coder",
        agent="codex",
        agent_args=["--model", "gpt-own", "-c", 'model_reasoning_effort="low"'],
    )
    command = _command(tmux)
    native = command[command.index("--") + 1 :]
    native = native[: native.index("--")] if "--" in native else native
    read = _codex(binary, home, native)
    assert read.returncode == 0, (native, read.stderr)
    assert native.count("--model") == 1 and "gpt-role" not in native, native
