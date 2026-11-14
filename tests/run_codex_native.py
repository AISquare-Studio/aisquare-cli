"""Run the mandatory native CI fixture; an all-skipped pytest run is a failure.

``python -m tests.run_codex_native --version <v>`` checks that the ``codex`` on PATH is
``<v>``, one of :data:`CODEX_VERSIONS` (the CI matrix runs each), then runs
``tests/test_codex_native.py`` with ``AISQUARE_TEST_CODEX=1``, and fails unless a test ran
that was not skipped, and none failed. ``--any-version`` is the nightly leg's: whatever
``latest`` installed, said in the log, so Codex's drift shows before a user meets it.
``--no-skips`` is the macOS leg's: there Codex's sandbox starts a command, so its sandbox and
automatic-reviewer tests run, and one skipped fails the leg.
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

#: The Codex releases the beta is held to (R7 of 0.10.0): 0.164.0, its floor, 0.166.0, which
#: the owner runs, and 0.167.0, the current one, which a new user gets (#112). CI runs the
#: suite on each.
CODEX_VERSIONS = ("0.164.0", "0.166.0", "0.167.0")
NATIVE_MODULE = Path(__file__).with_name("test_codex_native.py")


def installed_version() -> str:
    """The version ``codex --version`` prints (``codex-cli 0.166.0``), its last word."""
    shown = subprocess.run(["codex", "--version"], check=True, text=True, capture_output=True)
    words = shown.stdout.split()
    return words[-1] if words else ""


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.run_codex_native")
    chosen = parser.add_mutually_exclusive_group(required=True)
    chosen.add_argument(
        "--version", choices=CODEX_VERSIONS, help="the release the matrix leg installed"
    )
    chosen.add_argument(
        "--any-version",
        action="store_true",
        help="run on whatever codex is installed (the nightly leg on latest)",
    )
    parser.add_argument(
        "--no-skips",
        action="store_true",
        help="fail when any test skipped (every CI leg: one whose sandbox cannot run them fails)",
    )
    asked = parser.parse_args(argv)
    version = installed_version()
    if not asked.any_version and version != asked.version:
        raise RuntimeError(f"Expected Codex {asked.version}, got {version!r}")
    print(f"codex-cli {version}", flush=True)
    with tempfile.TemporaryDirectory(prefix="asq-codex-check-") as directory:
        report = Path(directory) / "results.xml"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-ra",
                "--durations=0",
                str(NATIVE_MODULE),
                f"--junitxml={report}",
            ],
            env={**os.environ, "AISQUARE_TEST_CODEX": "1"},
        )
        if result.returncode:
            return result.returncode
        cases = [
            case
            for case in ET.parse(report).findall(".//testcase")
            if case.get("classname") == f"tests.{NATIVE_MODULE.stem}"
        ]
        if not cases:
            raise RuntimeError(f"No test cases reported for {NATIVE_MODULE.name}")
        if any(case.find(tag) is not None for case in cases for tag in ("failure", "error")):
            raise RuntimeError(f"Native Codex tests failed in {NATIVE_MODULE.name}")
        if not any(case.find("skipped") is None for case in cases):
            raise RuntimeError(f"Native Codex tests all skipped in {NATIVE_MODULE.name}")
        skipped = [case.get("name") for case in cases if case.find("skipped") is not None]
        if asked.no_skips and skipped:
            names = ", ".join(map(str, skipped))
            raise RuntimeError(f"Native Codex tests skipped where this leg runs them all: {names}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
