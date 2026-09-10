"""The native CI job must prove a pinned binary actually ran the fixture."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests import run_codex_native


@pytest.mark.parametrize("outcome", ["pass", "skip", "failure", "empty", "wrong-test"])
def test_native_runner_rejects_green_pytest_runs_without_native_assertions(
    outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def execute(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command == ["codex", "--version"]:
            return subprocess.CompletedProcess(command, 0, stdout=run_codex_native.CODEX_VERSION)
        assert kwargs["env"]["AISQUARE_TEST_CODEX"] == "1"  # type: ignore[index]
        assert any(arg.endswith("test_codex_native.py") for arg in command)
        path = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml=")))
        name = "wrong" if outcome == "wrong-test" else run_codex_native.NATIVE_TEST
        child = (
            '<skipped message="gate drifted"/>'
            if outcome == "skip"
            else "<failure/>"
            if outcome == "failure"
            else ""
        )
        case = "" if outcome == "empty" else f'<testcase name="{name}">{child}</testcase>'
        path.write_text(f"<testsuites><testsuite>{case}</testsuite></testsuites>")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.delenv("AISQUARE_TEST_CODEX", raising=False)
    monkeypatch.setattr(subprocess, "run", execute)
    if outcome == "pass":
        assert run_codex_native.run() == 0
    else:
        with pytest.raises(RuntimeError, match="must execute and pass"):
            run_codex_native.run()


def test_native_runner_refuses_a_different_codex_version(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def execute(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="codex-cli 0.0.0")

    monkeypatch.setattr(subprocess, "run", execute)
    with pytest.raises(RuntimeError, match="Expected codex-cli"):
        run_codex_native.run()
    assert calls == [["codex", "--version"]]


def test_ci_requires_the_native_runner_and_a_parent_agent_environment() -> None:
    text = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text()
    native = re.search(r"^  codex:\n(.*?)(?=^  \w+:|\Z)", text, re.M | re.S)
    ambient = re.search(r"^  ambient:\n(.*?)(?=^  \w+:|\Z)", text, re.M | re.S)
    assert native and ambient
    assert re.search(r"^          python -m tests.run_codex_native$", native[1], re.M)
    for variable in (
        "CODEX_HOME",
        "AISQUARE_CODING_AGENT",
        "AISQUARE_LAUNCH_ID",
        "AISQUARE_FLEET_AGENT",
    ):
        assert f'echo "{variable}=' in ambient[1]
