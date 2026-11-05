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
            return subprocess.CompletedProcess(command, 0, stdout="codex-cli 0.166.0\n")
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
        assert run_codex_native.run(["--version", "0.166.0"]) == 0
    else:
        with pytest.raises(RuntimeError, match=message):
            run_codex_native.run(["--version", "0.166.0"])


@pytest.mark.parametrize(
    "argv", [["--version", "0.164.0"], []], ids=["another release", "none named"]
)
def test_native_runner_refuses_a_codex_it_was_not_told_to_run(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leg runs the release it installed and names (``--version``, one of the beta's), or
    says it runs whatever is there (``--any-version``, the nightly's); a leg that names
    neither, or another release than the one it got, proves nothing and fails."""
    calls: list[list[str]] = []

    def execute(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="codex-cli 0.166.0")

    monkeypatch.setattr(subprocess, "run", execute)
    if argv:
        with pytest.raises(RuntimeError, match=re.escape("Expected Codex 0.164.0, got '0.166.0'")):
            run_codex_native.run(argv)
        assert calls == [["codex", "--version"]]
    else:
        with pytest.raises(SystemExit):
            run_codex_native.run(argv)
        assert calls == []


def _job(text: str, name: str) -> str:
    found = re.search(rf"^  {name}:\n(.*?)(?=^  [\w-]+:|\Z)", text, re.M | re.S)
    assert found, name
    return found[1]


def test_ci_runs_the_native_suite_on_every_release_the_beta_holds_and_latest_nightly() -> None:
    """R7 of 0.10.0: the ``codex`` job is a matrix of the beta's releases
    (``run_codex_native.CODEX_VERSIONS``: the floor and the current one), each installed and
    named to the runner, both required by ``ci-ok``; ``codex-latest`` runs the newest release
    nightly and by hand with ``--any-version``, and decides no other run."""
    text = _strip_comments(
        (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text()
    )
    native = _job(text, "codex")
    matrix = re.search(r"^        codex: \[(.*)\]$", native, re.M)
    assert matrix and tuple(v.strip(' "') for v in matrix[1].split(",")) == (
        run_codex_native.CODEX_VERSIONS
    )
    assert re.search(r"^ +npm install .+ @openai/codex@\$\{\{ matrix.codex \}\}$", native, re.M)
    assert re.search(
        r"^ +python -m tests.run_codex_native --version \$\{\{ matrix.codex \}\}$", native, re.M
    )
    latest = _job(text, "codex-latest")
    assert re.search(r"^ +npm install .+ @openai/codex@latest$", latest, re.M)
    assert re.search(r"^ +run: python -m tests.run_codex_native --any-version$", latest, re.M)
    gate = "github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'"
    assert latest.count(f"if: {gate}") >= 4, "every step but the notice runs only then"
    needs = re.search(r"^    needs: \[(.*)\]$", _job(text, "ci-ok"), re.M)
    assert needs and {"codex", "codex-latest"} <= {n.strip() for n in needs[1].split(",")}
