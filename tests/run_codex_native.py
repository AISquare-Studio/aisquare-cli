"""Run the mandatory native CI fixture; an all-skipped pytest run is a failure."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

CODEX_VERSION = "codex-cli 0.153.4"
NATIVE_TEST = "test_real_codex_hooks_resume_and_usage"


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
                str(Path(__file__).with_name("test_codex_native.py")),
                f"--junitxml={report}",
            ],
            env={**os.environ, "AISQUARE_TEST_CODEX": "1"},
        )
        if result.returncode:
            return result.returncode
        cases = ET.parse(report).findall(".//testcase")
        if not any(case.get("name") == NATIVE_TEST for case in cases) or any(
            case.find(tag) is not None for case in cases for tag in ("skipped", "failure", "error")
        ):
            raise RuntimeError("Native Codex fixture must execute and pass without skips")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
