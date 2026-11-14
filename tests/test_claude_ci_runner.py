"""Claude Code's native CI jobs prove a pinned ``claude`` ran the suite, and coverage holds.

The runner (``tests/run_claude_native.py``) fails a pytest run that proves nothing: a module
with no case, all skipped, or one failed; the ``claude`` job pins the version the runner
expects, on ubuntu and macOS; ``claude-latest`` runs the newest release only nightly and by
hand, never on a pull request; and the py3.12 leg's coverage reaches the ``coverage`` job,
whose script (``tests/coverage_floors.py``) fails under a floor or with a shard missing.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests import coverage_floors, run_claude_native
from tests.ci_matrix import jobs, workflow
from tests.test_ci_covers_the_ambient_environment import _strip_comments

CI = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"


def _job(name: str) -> str:
    text = _strip_comments(CI.read_text())
    found = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  [\w-]+:|\Z)", text, re.M | re.S)
    assert found, name
    return found[1]


@pytest.mark.parametrize(
    "outcome,message",
    [
        ("pass", None),
        ("xfail", None),
        ("skip", "all skipped"),
        ("failure", "tests failed"),
        ("error", "tests failed"),
        ("one-module-missing", "No test cases"),
        ("wrong-module", "No test cases"),
    ],
)
def test_native_runner_rejects_green_pytest_runs_without_native_assertions(
    outcome: str, message: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def execute(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command == ["claude", "--version"]:
            assert kwargs["env"]["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
            return subprocess.CompletedProcess(command, 0, stdout="2.1.318 (Claude Code)\n")
        assert kwargs["env"]["AISQUARE_TEST_CLAUDE"] == "1"
        assert [a for a in command if a.endswith(".py")] == [
            str(m) for m in run_claude_native.NATIVE_MODULES
        ]
        path = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml=")))
        cases = ""
        for index, module in enumerate(run_claude_native.NATIVE_MODULES):
            if outcome == "one-module-missing" and index == 0:
                continue
            classname = "tests.wrong" if outcome == "wrong-module" else f"tests.{module.stem}"
            child = {
                "skip": '<skipped message="gate drifted"/>',
                "failure": "<failure/>",
                "error": "<error/>",
            }.get(outcome, "")
            cases += f'<testcase classname="{classname}" name="test_native">{child}</testcase>'
            if outcome == "xfail":  # a strict xfail is a skip beside the module's passes
                cases += f'<testcase classname="{classname}" name="test_f"><skipped/></testcase>'
        path.write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.delenv("AISQUARE_TEST_CLAUDE", raising=False)
    monkeypatch.setattr(subprocess, "run", execute)
    if message is None:
        assert run_claude_native.run([]) == 0
    else:
        with pytest.raises(RuntimeError, match=message):
            run_claude_native.run([])


def test_no_skips_fails_a_leg_where_one_native_test_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each Claude leg runs the whole suite, so one test that skipped while the rest of its
    module passed proves less than the leg says, and fails it, named; a strict xfail is no
    skip (review r40 of #113, finding 14). Without the flag, as before: only a module that
    skipped every test fails."""

    def execute(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command == ["claude", "--version"]:
            return subprocess.CompletedProcess(command, 0, stdout="2.1.318 (Claude Code)\n")
        path = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml=")))
        cases = ""
        for module in run_claude_native.NATIVE_MODULES:
            name = f"tests.{module.stem}"
            cases += f'<testcase classname="{name}" name="ran"/>'
            cases += (
                f'<testcase classname="{name}" name="test_f">'
                '<skipped type="pytest.xfail" message="strict"/></testcase>'
            )
            if module.stem == "test_claude_native_transcript":
                cases += (
                    f'<testcase classname="{name}" name="test_npm">'
                    '<skipped type="pytest.skip" message="not an npm install"/></testcase>'
                )
        path.write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", execute)
    assert run_claude_native.run([]) == 0
    with pytest.raises(
        RuntimeError, match=r"test_claude_native_transcript\.py, where this leg runs"
    ):
        run_claude_native.run(["--no-skips"])
    try:
        run_claude_native.run(["--no-skips"])
    except RuntimeError as exc:
        assert str(exc).endswith(": test_npm"), "named, and the xfail is not"


def test_native_runner_refuses_another_claude_unless_any_version_is_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def execute(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command == ["claude", "--version"]:
            return subprocess.CompletedProcess(command, 0, stdout="2.1.400 (Claude Code)\n")
        return subprocess.CompletedProcess(command, 3)

    monkeypatch.setattr(subprocess, "run", execute)
    with pytest.raises(RuntimeError, match=r"Expected Claude Code 2\.1\.318"):
        run_claude_native.run([])
    assert calls == [["claude", "--version"]]
    assert run_claude_native.run(["--any-version"]) == 3, "the suite ran on 2.1.400"


def test_the_native_suite_is_every_claude_native_module() -> None:
    names = {module.name for module in run_claude_native.NATIVE_MODULES}
    assert names == {p.name for p in Path(__file__).parent.glob("test_claude_native*.py")}
    assert "test_claude_native.py" in names and len(names) >= 4


def test_ci_runs_the_pinned_claude_on_ubuntu_and_macos_with_the_runner() -> None:
    job = _job("claude")
    assert re.search(r"^          python -m tests\.run_claude_native --no-skips$", job, re.M), job
    install = re.search(r"npm install .+ @anthropic-ai/claude-code@(\S+)$", job, re.M)
    assert install and install[1] == run_claude_native.CLAUDE_VERSION
    assert f"({run_claude_native.CLAUDE_VERSION}," in job, "the job's name says the pin"
    claude = workflow()["jobs"]["claude"]
    assert claude["strategy"]["matrix"]["os"] == ["ubuntu-latest", "macos-latest"]
    assert "if" not in claude, "the merge gate runs on every event"


def test_the_latest_leg_runs_nightly_and_by_hand_and_passes_untested_elsewhere() -> None:
    latest = workflow()["jobs"]["claude-latest"]
    assert "if" not in latest, "a skipped job fails ci-ok"
    nightly = "github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'"
    steps = latest["steps"]
    assert steps[0]["if"] == (
        "github.event_name != 'schedule' && github.event_name != 'workflow_dispatch'"
    )
    assert all(step["if"] == nightly for step in steps[1:]), steps
    assert steps[-1]["run"] == "python -m tests.run_claude_native --any-version --no-skips"
    assert "@anthropic-ai/claude-code@latest" in steps[-2]["run"]
    triggers = workflow()[True]  # YAML 1.1 reads the `on:` key as True
    assert "schedule" in triggers and "workflow_dispatch" in triggers


def test_ci_ok_needs_both_claude_jobs_and_the_coverage() -> None:
    needs = workflow()["jobs"]["ci-ok"]["needs"]
    assert {"claude", "claude-latest", "coverage"} <= set(needs)


def test_the_py312_leg_measures_coverage_and_hands_every_shard_to_the_coverage_job() -> None:
    test = workflow()["jobs"]["test"]
    lines = [step["run"] for step in test["steps"] if "pytest -n auto" in str(step.get("run"))]
    assert lines and all(
        "${{ matrix.leg == 'py3.12' && '--cov --cov-report=' || '' }}" in line for line in lines
    )
    keep = "Keep this shard's coverage data for the coverage job"
    [kept] = [step for step in test["steps"] if step.get("name") == keep]
    assert kept["if"] == "${{ matrix.leg == 'py3.12' && !cancelled() }}"
    assert '/shards/${{ matrix.leg }}/${{ matrix.shard }}.coverage"' in kept["run"]
    uploaded = next(
        at for at, step in enumerate(test["steps"]) if "upload-artifact" in str(step.get("uses"))
    )
    assert test["steps"].index(kept) < uploaded, "kept before the shard's note is uploaded"
    coverage = workflow()["jobs"]["coverage"]
    assert coverage["needs"] == "test"
    fetched = coverage["steps"][-2]["with"]
    assert fetched["pattern"] == "shard-py3.12-*" and fetched["merge-multiple"] is True
    for lean in (True, False):  # the leg runs on a sub-PR too, in as many shards
        [shards] = {leg["shards"] for leg in jobs("test", lean=lean) if leg["leg"] == "py3.12"}
        assert coverage["steps"][-1]["run"] == (
            f'python -m tests.coverage_floors "$RUNNER_TEMP/shards/py3.12" --shards {shards}'
        )


# --- tests/coverage_floors.py ---------------------------------------------------------------


def _report(**modules: tuple[int, int, int, int]) -> dict[str, Any]:
    """A coverage json report: each module's statements, covered, branches, covered."""
    files = {}
    for name, (statements, covered, branches, covered_branches) in modules.items():
        path = name.replace("__", "/") + ".py"
        files[path] = {
            "summary": {
                "num_statements": statements,
                "covered_lines": covered,
                "num_branches": branches,
                "covered_branches": covered_branches,
            },
            "executed_lines": list(range(1, covered + 1)),
            "missing_lines": list(range(covered + 1, statements + 1)),
        }
    return {"files": files}


CLAUDE = "src__aisquare__core__agent_adapters__claude_code"
FLEET = "src__aisquare__services__fleet"
CODEX = "src__aisquare__core__agent_adapters__codex"


def test_the_floors_hold_claude_codes_adapter_and_the_seam_and_report_the_rest() -> None:
    report = _report(
        **{CLAUDE: (100, 90, 40, 30), FLEET: (200, 150, 80, 40), CODEX: (50, 1, 10, 0)}
    )
    found = coverage_floors.figures(report)
    assert set(found) == {f"{n.replace('__', '/')}.py" for n in (CLAUDE, FLEET, CODEX)}
    claude, fleet = f"{CLAUDE.replace('__', '/')}.py", f"{FLEET.replace('__', '/')}.py"
    assert (found[claude].line, found[claude].branch) == (90.0, 75.0)
    held = {claude: (89.0, 74.0), fleet: (74.0, 49.0)}
    missing = [f"{path}: not measured at all" for path in coverage_floors.SEAM if path != fleet]
    assert coverage_floors.problems(found, held) == sorted(missing)
    under = {claude: (90.5, 74.0), fleet: (74.0, 50.5)}
    said = coverage_floors.problems(found, under)
    assert len(said) == len(set(said)), "each line once (tester 846e8c78's nit)"
    assert f"{claude}: line coverage 90.0% is under its floor 90.5%" in said
    assert f"{fleet}: branch coverage 50.0% is under its floor 50.5%" in said
    assert not any("codex.py" in line for line in said), "Codex's adapter is reported, not held"
    assert "| `core/agent_adapters/codex.py` | 1/50 |" in coverage_floors.table(found, held)


def test_the_uncovered_regions_are_runs_of_claude_codes_statements_largest_first() -> None:
    report = _report(**{CLAUDE: (10, 0, 0, 0), CODEX: (50, 0, 0, 0)})
    entry = report["files"][f"{CLAUDE.replace('__', '/')}.py"]
    entry["executed_lines"], entry["missing_lines"] = [4, 9], [1, 2, 3, 5, 6, 7, 8, 10]
    assert coverage_floors.regions(report) == [
        ("core/agent_adapters/claude_code.py:5-8", 4),
        ("core/agent_adapters/claude_code.py:1-3", 3),
        ("core/agent_adapters/claude_code.py:10-10", 1),
    ]


def test_a_shard_missing_fails_the_coverage_rather_than_reading_part_of_the_suite(
    tmp_path: Path,
) -> None:
    for shard in (1, 2, 4):
        (tmp_path / f"{shard}.coverage").write_text("")
    with pytest.raises(SystemExit, match=r"no data from 3\.coverage"):
        coverage_floors.combine(tmp_path, 4, tmp_path / "out")


def test_every_floor_names_a_module_it_holds() -> None:
    assert all(coverage_floors.floored(path) for path in coverage_floors.FLOORS)
    assert json.dumps(sorted(coverage_floors.FLOORS))  # paths, in the report's spelling


def test_every_held_module_has_its_floor() -> None:
    """Claude Code's adapter modules and the seam, each held: a new ``claude_*.py`` gets a
    floor in the commit that adds it, and none is dropped quietly."""
    root = Path(__file__).resolve().parents[1]
    held = {
        f"src/aisquare/core/agent_adapters/{p.name}"
        for p in (root / "src/aisquare/core/agent_adapters").glob("claude_*.py")
    } | set(coverage_floors.SEAM)
    assert set(coverage_floors.FLOORS) == held
    assert all(
        0 < line <= 100 and 0 < branch <= 100 for line, branch in coverage_floors.FLOORS.values()
    )
