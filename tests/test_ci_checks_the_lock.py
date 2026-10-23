"""CI fails a change whose ``uv.lock`` no longer matches ``pyproject.toml`` (#278).

The lock drifted for days once: ``pyyaml``, required since the persona core (#201),
was missing from it, so ``uv run --frozen`` raised ``ModuleNotFoundError: yaml`` on
every persona path, and nothing noticed, because CI installs with pip. Then the
``voice`` extra landed without a relock. ``uv lock --check`` in CI and in
``make check`` is what notices the next one.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def _steps(job: str) -> list[dict[str, Any]]:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    steps: list[dict[str, Any]] = workflow["jobs"][job]["steps"]
    return steps


def test_ci_checks_the_lock_with_a_pinned_uv() -> None:
    steps = _steps("lock")
    setup = [step for step in steps if str(step.get("uses", "")).startswith("astral-sh/setup-uv@")]
    assert len(setup) == 1, "the lock job installs uv with astral-sh/setup-uv"
    assert re.fullmatch(r"\d+\.\d+\.\d+", str(setup[0].get("with", {}).get("version", ""))), (
        "uv itself is pinned, so a uv release cannot turn the check red on its own"
    )
    assert any(step.get("run", "").strip() == "uv lock --check" for step in steps)


def test_make_check_runs_the_lock_check() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    check = re.search(r"^check:(.*)$", makefile, re.MULTILINE)
    assert check is not None and "lockcheck" in check.group(1).split()
    recipe = re.search(r"^lockcheck:\n((?:\t.*\n)+)", makefile, re.MULTILINE)
    assert recipe is not None and "uv lock --check" in recipe.group(1)


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")
def test_the_lock_matches_pyproject() -> None:
    result = subprocess.run(
        ["uv", "lock", "--check", "--offline"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if "offline" in result.stderr.lower() and "network" in result.stderr.lower():
        pytest.skip("uv's cache cannot resolve offline here; CI's lock job runs it online")
    assert result.returncode == 0, result.stderr
