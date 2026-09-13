"""The native CI job must prove a pinned binary actually ran the fixture."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests import run_codex_native
from tests.test_ci_covers_the_ambient_environment import _strip_comments


@pytest.mark.parametrize(
    "outcome,message",
    [
        ("pass", None),
        ("renamed", None),
        ("parameterized", None),
        ("other-skipped", None),
        ("skip", "all skipped"),
        ("failure", "tests failed"),
        ("empty", "No test cases"),
        ("wrong-module", "No test cases"),
    ],
)
def test_native_runner_rejects_green_pytest_runs_without_native_assertions(
    outcome: str, message: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def execute(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command == ["codex", "--version"]:
            return subprocess.CompletedProcess(command, 0, stdout=run_codex_native.CODEX_VERSION)
        assert kwargs["env"]["AISQUARE_TEST_CODEX"] == "1"  # type: ignore[index]
        assert any(arg.endswith("test_codex_native.py") for arg in command)
        path = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml=")))
        name = "test_renamed" if outcome == "renamed" else "test_native"
        if outcome == "parameterized":
            name += "[fixture]"
        module = "tests.wrong" if outcome == "wrong-module" else "tests.test_codex_native"
        child = (
            '<skipped message="gate drifted"/>'
            if outcome == "skip"
            else "<failure/>"
            if outcome == "failure"
            else ""
        )
        case = (
            ""
            if outcome == "empty"
            else f'<testcase classname="{module}" name="{name}">{child}</testcase>'
        )
        if outcome == "other-skipped":
            case += (
                '<testcase classname="tests.test_codex_native" name="test_optional">'
                "<skipped/></testcase>"
            )
            case += (
                '<testcase classname="tests.unrelated" name="test_skipped"><skipped/></testcase>'
            )
        path.write_text(f"<testsuites><testsuite>{case}</testsuite></testsuites>")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.delenv("AISQUARE_TEST_CODEX", raising=False)
    monkeypatch.setattr(subprocess, "run", execute)
    if message is None:
        assert run_codex_native.run() == 0
    else:
        with pytest.raises(RuntimeError, match=message):
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


def test_ci_requires_the_native_runner_and_the_same_version_pin() -> None:
    text = _strip_comments(
        (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text()
    )
    native = re.search(r"^  codex:\n(.*?)(?=^  \w+:|\Z)", text, re.M | re.S)
    assert native
    assert re.search(r"^          python -m tests.run_codex_native$", native[1], re.M)
    install = re.search(r"^          npm install .+ @openai/codex@(\S+)$", native[1], re.M)
    assert install and f"codex-cli {install[1]}" == run_codex_native.CODEX_VERSION
