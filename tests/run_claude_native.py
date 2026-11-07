"""Run the native Claude Code suite in CI; a run that proves nothing is a failure.

``python -m tests.run_claude_native`` checks that the ``claude`` on PATH is the pinned
version, then runs every ``tests/test_claude_native*.py`` module with
``AISQUARE_TEST_CLAUDE=1``, and fails unless each module ran at least one test that was
not skipped, and none failed. ``--any-version`` is the scheduled leg's: whatever ``latest``
installed, said in the log, so Claude Code's drift shows before a user meets it.
``--no-skips`` is every Claude leg's: each runs the whole suite on an npm install, so one
test that skipped proves less than the leg says, and fails it, named (review r40 of #113,
finding 14). A strict ``xfail`` is an expected failure, not a skip.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path

#: The Claude Code the owner runs (2026-11-05), the one every PR is tested on.
CLAUDE_VERSION = "2.1.318"
NATIVE_MODULES = tuple(sorted(Path(__file__).parent.glob("test_claude_native*.py")))
#: Claude Code's own calls besides the model's, off for the version check as in the suite.
QUIET = {
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}


def installed_version() -> str:
    """The version ``claude --version`` prints (``2.1.318 (Claude Code)``), its first word."""
    shown = subprocess.run(
        ["claude", "--version"],
        check=True,
        text=True,
        capture_output=True,
        env={**os.environ, **QUIET},
    ).stdout.strip()
    return shown.split()[0] if shown else ""


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.run_claude_native")
    parser.add_argument(
        "--any-version",
        action="store_true",
        help="run on whatever claude is installed (the scheduled leg on latest)",
    )
    parser.add_argument(
        "--no-skips",
        action="store_true",
        help="fail when any test skipped (every Claude leg: each runs them all)",
    )
    asked = parser.parse_args(argv)
    version = installed_version()
    if not asked.any_version and version != CLAUDE_VERSION:
        raise RuntimeError(f"Expected Claude Code {CLAUDE_VERSION}, got {version!r}")
    print(f"Claude Code {version}", flush=True)
    if not NATIVE_MODULES:
        raise RuntimeError("No tests/test_claude_native*.py modules")
    with tempfile.TemporaryDirectory(prefix="asq-claude-check-") as directory:
        report = Path(directory) / "results.xml"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-ra",
                "--durations=0",
                *(str(module) for module in NATIVE_MODULES),
                f"--junitxml={report}",
            ],
            env={**os.environ, "AISQUARE_TEST_CLAUDE": "1"},
        )
        if result.returncode:
            return result.returncode
        cases = ET.parse(report).findall(".//testcase")
        for module in NATIVE_MODULES:
            own = [case for case in cases if case.get("classname") == f"tests.{module.stem}"]
            if not own:
                raise RuntimeError(f"No test cases reported for {module.name}")
            if any(case.find(tag) is not None for case in own for tag in ("failure", "error")):
                raise RuntimeError(f"Native Claude Code tests failed in {module.name}")
            if not any(case.find("skipped") is None for case in own):
                raise RuntimeError(f"Native Claude Code tests all skipped in {module.name}")
            skipped = [
                str(case.get("name"))
                for case in own
                if (mark := case.find("skipped")) is not None and mark.get("type") != "pytest.xfail"
            ]
            if asked.no_skips and skipped:
                raise RuntimeError(
                    f"Native Claude Code tests skipped in {module.name}, where this leg runs "
                    f"them all: {', '.join(skipped)}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
