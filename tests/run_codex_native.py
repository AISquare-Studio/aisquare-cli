"""Run the mandatory native CI fixture; an all-skipped pytest run is a failure."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

CODEX_VERSION = "codex-cli 0.153.4"
NATIVE_MODULE = Path(__file__).with_name("test_codex_native.py")


def run() -> int:
    version = subprocess.run(["codex", "--version"], check=True, text=True, capture_output=True)
    if version.stdout.strip() != CODEX_VERSION:
        raise RuntimeError(f"Expected {CODEX_VERSION}, got {version.stdout.strip()!r}")
    print(version.stdout.strip(), flush=True)
    with tempfile.TemporaryDirectory(prefix="asq-codex-check-") as directory:
        report = Path(directory) / "results.xml"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-ra",
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
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
