"""A project's own ``aisquare/`` package must not shadow the CLI re-invoking itself (#81).

``python -m aisquare`` puts the current directory FIRST on ``sys.path``. From a
repo whose root holds a top-level ``aisquare/`` package — the explainability
SDK's own repo ships one, and any customer repo may — the interpreter finds that
package, not ours, and dies: "No module named aisquare.__main__; 'aisquare' is a
package and cannot be directly executed". Every self-invocation the CLI makes
went through that door: ``init``/``doctor``/``project onboard`` from the fleet
UI, every fleet window, the detached distiller, and the hook fallback command.

The fix is ONE builder — :func:`aisquare.core.selfcli.argv_for` — that passes
the interpreter ``-P`` (the flag form of ``PYTHONSAFEPATH``, Python 3.11+, which
this CLI requires). A flag and not a variable, because the fleet's ``launch``
``execve``s the agent with the window's whole environment and a coder's own
``python -m pytest`` must not inherit a changed ``sys.path``. This file pins the
builder, the one caller a fake tmux cannot reach (the distiller), the hook
string's round trip through its own matcher, the doctor row — and, through a
real subprocess, that the shadow is REAL and the flag defeats it.

Measured while writing this: the two shadow shapes fail DIFFERENTLY. A package
(``aisquare/__init__.py``) dies with the error above, exit 1. A sibling MODULE
(``aisquare.py``) is simply run as ``__main__`` — exit 0, our version never
printed, the project's code executed in our name. So the premise below is not
"the bare form fails" but "the bare form runs the project's code, not ours".
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import agents, selfcli
from aisquare.core.version import __version__
from aisquare.models import CheckStatus
from aisquare.services import diagnostics
from aisquare.services import distill as _distill

#: The REAL ``spawn_drain``, bound at import: conftest's autouse
#: ``no_detached_distill`` swaps the module attribute for a no-op before every
#: test, and this file exists to inspect what the real one would launch.
_REAL_SPAWN_DRAIN = _distill.spawn_drain

#: The two shapes that shadow a ``-m`` import from the cwd. A bare ``aisquare/``
#: directory is NOT one of them — it is a PEP 420 namespace portion, and a real
#: package anywhere on ``sys.path`` beats it — so the doctor test below checks
#: that a bare directory stays green.
SHADOWS = ("aisquare/__init__.py", "aisquare.py")

#: What the shadow prints when it runs — proof the PROJECT's code was executed.
MARKER = "SHADOWED BY THE PROJECT"


def _shadowing_project(tmp_path: Path, shape: str) -> Path:
    path = tmp_path / shape
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"print({MARKER!r})\n", encoding="utf-8")
    return tmp_path


def _without_the_workaround() -> dict[str, str]:
    """The operator's own ``PYTHONSAFEPATH=1`` export must not carry this test."""
    return {key: value for key, value in os.environ.items() if key != "PYTHONSAFEPATH"}


def test_argv_for_passes_safe_path_to_the_interpreter() -> None:
    assert selfcli.argv_for(["--json", "init"]) == [
        sys.executable,
        "-P",
        "-m",
        "aisquare",
        "--json",
        "init",
    ]


@pytest.mark.parametrize("shape", SHADOWS)
def test_a_project_with_its_own_aisquare_cannot_shadow_the_self_invocation(
    tmp_path: Path, shape: str
) -> None:
    """Measured through the real seam, from a directory that reproduces #81."""
    project = _shadowing_project(tmp_path, shape)
    env = _without_the_workaround()

    # The premise, asserted: WITHOUT the flag this directory shadows us — the
    # project's code runs and ours does not. Without this the test would pass
    # over a fixture that never reproduced the bug.
    bare = subprocess.run(
        [sys.executable, "-m", "aisquare", "--version"],
        cwd=project,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert MARKER in bare.stdout, f"{shape} did not shadow `python -m aisquare`: {bare}"
    assert f"aisquare {__version__}" not in bare.stdout, bare
    if shape.endswith("__init__.py"):
        assert bare.returncode != 0 and "No module named aisquare.__main__" in bare.stderr
    else:
        assert bare.returncode == 0, "a sibling module is RUN, silently — the worse shape"

    result = selfcli.run(["--version"], cwd=project, env=env, timeout=120.0)

    assert result.ok, result.stderr
    assert result.stdout.strip() == f"aisquare {__version__}"


def test_the_detached_distiller_is_built_by_the_same_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The distiller runs with ``cwd=<project root>`` — exactly where the shadow lives."""
    seen: dict[str, Any] = {}

    def fake_popen(argv: list[str], **kwargs: Any) -> Any:
        seen.update(argv=argv, cwd=kwargs.get("cwd"))
        return None

    monkeypatch.setattr("aisquare.core.brain.brain_enabled", lambda: True)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/gbrain")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    _REAL_SPAWN_DRAIN(root=tmp_path)

    assert seen["cwd"] == str(tmp_path)
    assert seen["argv"] == selfcli.argv_for(["--quiet", "team", "distill"])
    assert seen["argv"][:4] == [sys.executable, "-P", "-m", "aisquare"]


def test_the_hook_fallback_carries_the_flag_and_still_round_trips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hooks run with the PROJECT as cwd — the one place the shadow is guaranteed.

    The fallback must carry ``-P``, and ``_is_aisquare_hook_command`` must keep
    recognising both the new string and the one an earlier CLI wrote, or
    ``connect`` appends a duplicate and ``disconnect`` cannot remove the old one.
    """
    monkeypatch.setattr("aisquare.core.agents.sys.platform", "linux")
    monkeypatch.setattr("aisquare.core.agents.sys.argv", ["pytest"])
    monkeypatch.setattr("aisquare.core.agents.shutil.which", lambda _name: None)

    command = agents._aisquare_command()

    assert command == f"{shlex.quote(sys.executable)} -P -m aisquare"
    assert agents._is_aisquare_hook_command(f"{command} hook session-start")
    assert agents._is_aisquare_hook_command(
        f"{shlex.quote(sys.executable)} -m aisquare hook stop"
    ), "a hook written before -P is still ours"
    assert not agents._is_aisquare_hook_command(f"{command} webhook stop")


def test_doctor_warns_when_the_directory_would_shadow_a_hand_typed_module_run(
    tmp_path: Path,
) -> None:
    assert diagnostics._check_self_invocation(tmp_path).status is CheckStatus.ok

    (tmp_path / "aisquare").mkdir()
    bare_directory = diagnostics._check_self_invocation(tmp_path)
    assert bare_directory.status is CheckStatus.ok, "a namespace portion loses to the real package"

    _shadowing_project(tmp_path, "aisquare/__init__.py")
    warned = diagnostics._check_self_invocation(tmp_path)

    assert warned.status is CheckStatus.warn
    assert "aisquare/__init__.py" in warned.detail and "python -m aisquare" in warned.detail
    assert warned.fix is not None and "python -P -m aisquare" in warned.fix


@pytest.mark.parametrize("shape", SHADOWS)
def test_the_row_reaches_doctor_for_the_project_it_was_asked_about(
    tmp_path: Path, shape: str
) -> None:
    """Wired, not merely defined — the fleet UI passes the project root as ``cwd``."""
    project = _shadowing_project(tmp_path, shape)

    rows = {check.name: check for check in diagnostics.doctor(cwd=project)}

    assert rows["self-invocation"].status is CheckStatus.warn
    assert shape in rows["self-invocation"].detail
