"""CI runs each leg's suite in shards, and proves the shards ran it exactly once.

The suite is ~13k tests and about 65 minutes of one core; a 4-vCPU runner took 21-25
minutes a Linux leg and the full matrix about 65 (``.github/workflows/ci.yml``'s
header). So each leg is split across runners with pytest-split, and a split that
drops a test is green: nothing fails for a test that never ran. Each shard notes what
it collected and what it ran (``tests/shards.py``), and the ``shards`` job fails unless
every leg's shards add up to its collection exactly.

Three layers, pinned here: the check itself (synthetic notes, each way a split can go
wrong); the note, written by a real pytest with the real pytest-split, unsharded and
sharded, under xdist and not; and the workflow's wiring of both, with ``ci-ok`` as the
one check whose name does not move with the shards.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from tests import shards
from tests.ci_matrix import ROOT, jobs, workflow

#: Small, fast, and stable files of this suite for the real runs below to split.
_SAMPLE = ["tests/test_ci_concurrency.py", "tests/test_ci_checks_the_lock.py"]

_SPLIT = re.compile(r"--splits \$\{\{ matrix\.shards \}\} --group \$\{\{ matrix\.shard \}\}")

#: What every shard runs, the plain and the ambient step alike.
SHARD_PYTEST = (
    "pytest -n auto -ra --durations=20 --splits ${{ matrix.shards }} --group ${{ matrix.shard }}"
    " --splitting-algorithm least_duration"
    ' --shard-manifest "${{ runner.temp }}/shards/${{ matrix.leg }}/${{ matrix.shard }}.json"'
)


def _note(
    root: Path, leg: str, group: int, splits: int, collected: list[str], ran: list[str]
) -> None:
    shards.write(
        root / leg / f"{group}.json", splits=splits, group=group, collected=collected, selected=ran
    )


SUITE = [f"tests/test_x.py::test_{i}" for i in range(7)]


def test_a_split_that_runs_every_test_once_holds(tmp_path: Path) -> None:
    _note(tmp_path, "py3.12", 1, 3, SUITE, SUITE[:3])
    _note(tmp_path, "py3.12", 2, 3, SUITE, SUITE[3:5])
    _note(tmp_path, "py3.12", 3, 3, SUITE, SUITE[5:])
    (tmp_path / "py3.12" / "1.durations.json").write_text("{}", encoding="utf-8")

    problems, held = shards.check(tmp_path)

    assert problems == []
    assert held == ["py3.12: 3 shards ran the 7 collected tests, each exactly once (3, 2, 2)"]


def test_a_test_no_shard_ran_fails_and_is_named(tmp_path: Path) -> None:
    _note(tmp_path, "py3.12", 1, 2, SUITE, SUITE[:3])
    _note(tmp_path, "py3.12", 2, 2, SUITE, SUITE[4:])

    problems, _ = shards.check(tmp_path)

    assert problems == [f"py3.12: collected, and run by no shard (1): {SUITE[3]}"]


def test_a_test_two_shards_ran_fails_and_is_named(tmp_path: Path) -> None:
    """Twice is not merely slow: the split that ran it twice dropped something else, or
    a suite that relies on it running once is checked by luck."""
    _note(tmp_path, "py3.12", 1, 2, SUITE, SUITE[:4])
    _note(tmp_path, "py3.12", 2, 2, SUITE, SUITE[3:])

    problems, _ = shards.check(tmp_path)

    assert problems == [f"py3.12: run by more than one shard (1): {SUITE[3]}"]


def test_a_shard_with_no_note_fails(tmp_path: Path) -> None:
    """A shard that never reported ran nothing anyone can vouch for."""
    _note(tmp_path, "windows-py3.12", 1, 3, SUITE, SUITE[:3])
    _note(tmp_path, "windows-py3.12", 3, 3, SUITE, SUITE[3:])

    problems, _ = shards.check(tmp_path)

    assert problems == ["windows-py3.12: no note from shard(s) [2] of 3"]


def test_shards_that_collected_different_suites_fail(tmp_path: Path) -> None:
    """Each shard splits its OWN collection; two collections make two unrelated splits."""
    _note(tmp_path, "py3.12", 1, 2, SUITE, SUITE[:3])
    _note(tmp_path, "py3.12", 2, 2, SUITE[1:], SUITE[3:])

    problems, _ = shards.check(tmp_path)

    assert len(problems) == 1 and "collected different suites" in problems[0], problems


def test_shards_that_disagree_on_how_many_there_are_fail(tmp_path: Path) -> None:
    _note(tmp_path, "py3.12", 1, 2, SUITE, SUITE[:3])
    _note(tmp_path, "py3.12", 2, 3, SUITE, SUITE[3:])

    problems, _ = shards.check(tmp_path)

    assert len(problems) == 1 and "disagree" in problems[0], problems


def test_a_note_filed_under_another_shard_fails(tmp_path: Path) -> None:
    _note(tmp_path, "py3.12", 1, 2, SUITE, SUITE[:3])
    shards.write(tmp_path / "py3.12" / "2.json", splits=2, group=1, collected=SUITE, selected=[])

    problems, _ = shards.check(tmp_path)

    assert "py3.12: 2.json is the note of group 1" in problems


def test_a_test_run_and_never_collected_fails_and_is_named(tmp_path: Path) -> None:
    """A shard that ran something its collection never had split a different suite."""
    ghost = "tests/test_x.py::test_ghost"
    _note(tmp_path, "py3.12", 1, 2, SUITE, [*SUITE[:3], ghost])
    _note(tmp_path, "py3.12", 2, 2, SUITE, SUITE[3:])

    problems, _ = shards.check(tmp_path)

    assert problems == [f"py3.12: run, and never collected (1): {ghost}"]


def test_a_note_past_the_last_shard_fails(tmp_path: Path) -> None:
    _note(tmp_path, "py3.12", 1, 2, SUITE, SUITE[:3])
    _note(tmp_path, "py3.12", 2, 2, SUITE, SUITE[3:])
    _note(tmp_path, "py3.12", 3, 2, SUITE, [])

    problems, _ = shards.check(tmp_path)

    assert problems == ["py3.12: a note from shard(s) [3], past 2"]


@pytest.mark.parametrize(
    "content",
    ["{not json", "[]", '{"splits": 2, "group": 2, "selected": []}'],
    ids=["not-json", "not-an-object", "no-collection"],
)
def test_an_unreadable_note_fails_and_is_named(tmp_path: Path, content: str) -> None:
    _note(tmp_path, "py3.12", 1, 2, SUITE, SUITE[:3])
    (tmp_path / "py3.12" / "2.json").write_text(content, encoding="utf-8")

    problems, held = shards.check(tmp_path)

    assert held == []
    assert any(problem.startswith("py3.12/2.json: ") for problem in problems), problems


def test_a_leg_whose_every_note_is_unreadable_fails_beside_a_leg_that_holds(
    tmp_path: Path,
) -> None:
    """Such a leg never reaches the per-leg check, so only the read itself can fail it; and
    the other leg holding must not let the run pass."""
    _note(tmp_path, "py3.12", 1, 1, SUITE, SUITE)
    for group in (1, 2):
        (tmp_path / "py3.13").mkdir(exist_ok=True)
        (tmp_path / "py3.13" / f"{group}.json").write_text("{not json", encoding="utf-8")

    problems, held = shards.check(tmp_path)

    assert [line.split(":")[0] for line in held] == ["py3.12"]
    assert sorted(problem.split(":")[0] for problem in problems) == [
        "py3.13/1.json",
        "py3.13/2.json",
    ]
    assert shards.main(["check", str(tmp_path)]) == 1


def test_times_for_a_leg_with_none_are_refused(tmp_path: Path) -> None:
    """A refresh of .test_durations from a leg that left no times would write a file of
    nothing, which splits every test as an average one."""
    _note(tmp_path, "py3.12", 1, 1, SUITE, SUITE)
    with pytest.raises(SystemExit, match=r"no shard times for py3\.12"):
        shards.durations(tmp_path, ["py3.12"])


def test_no_notes_at_all_fail(tmp_path: Path) -> None:
    """A download that found nothing must not read as every leg holding."""
    problems, held = shards.check(tmp_path)

    assert held == [] and len(problems) == 1 and "no shard notes" in problems[0]


def test_each_leg_is_checked_on_its_own(tmp_path: Path) -> None:
    _note(tmp_path, "py3.12", 1, 1, SUITE, SUITE)
    _note(tmp_path, "py3.13", 1, 2, SUITE, SUITE[:2])
    _note(tmp_path, "py3.13", 2, 2, SUITE, SUITE[2:6])

    problems, held = shards.check(tmp_path)

    assert problems == [f"py3.13: collected, and run by no shard (1): {SUITE[6]}"]
    assert [line.split(":")[0] for line in held] == ["py3.12"]


def test_the_command_fails_on_a_bad_split_and_says_what_held(tmp_path: Path) -> None:
    _note(tmp_path, "py3.12", 1, 1, SUITE, SUITE)
    _note(tmp_path, "py3.13", 1, 1, SUITE, SUITE[1:])

    run = subprocess.run(
        [sys.executable, "-m", "tests.shards", "check", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert run.returncode == 1, run.stdout + run.stderr
    assert "shards complete and disjoint: py3.12: 1 shards ran the 7 collected tests" in run.stdout
    assert f"::error::py3.13: collected, and run by no shard (1): {SUITE[0]}" in run.stdout


def test_the_check_needs_nothing_installed() -> None:
    """The ``shards`` job runs it with the runner's bare python3: stdlib only at import."""
    blocked = ("pytest", "yaml", "aisquare", "pluggy")
    probe = (
        f"import sys; sys.modules.update(dict.fromkeys({blocked!r})); import tests.shards; "
        "sys.exit(tests.shards.main(['check', sys.argv[1]]))"
    )
    run = subprocess.run(
        [sys.executable, "-c", probe, str(ROOT / "nothing-here")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 1 and "no shard notes" in run.stdout, run.stdout + run.stderr


def _pytest(*args: str, sample: list[str] = _SAMPLE) -> subprocess.CompletedProcess[str]:
    """This suite's pytest on ``sample``, from the repository, as CI runs it."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", *args, *sample],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def _read(path: Path) -> dict[str, Any]:
    note: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return note


def test_real_shards_note_a_split_that_holds(tmp_path: Path) -> None:
    """pytest-split's own split of real tests, noted by the conftest's plugin, checked.

    And the union is the UNSHARDED collection: the same files run with no split note
    a suite the shards' notes agree with, so a shard's view of the suite is the suite.
    """
    whole = _pytest("--collect-only", "--shard-manifest", str(tmp_path / "whole" / "1.json"))
    assert whole.returncode == 0, whole.stdout + whole.stderr
    unsharded = _read(tmp_path / "whole" / "1.json")
    assert unsharded["selected"] == unsharded["collected"] and len(unsharded["collected"]) > 3
    notes = tmp_path / "notes"

    for group in (1, 2, 3):
        run = _pytest(
            "--collect-only",
            "--splits=3",
            f"--group={group}",
            "--splitting-algorithm=least_duration",
            "--shard-manifest",
            str(notes / "leg" / f"{group}.json"),
        )
        assert run.returncode == 0, run.stdout + run.stderr

    problems, held = shards.check(notes)

    assert problems == [], problems
    assert len(held) == 1, held
    split = [_read(notes / "leg" / f"{group}.json") for group in (1, 2, 3)]
    assert all(note["collected"] == unsharded["collected"] for note in split)
    assert sorted(i for note in split for i in note["selected"]) == sorted(unsharded["collected"])
    assert all(note["selected"] for note in split), "a shard of three tests or more ran none"


def test_a_shard_under_xdist_notes_its_split_and_its_times(tmp_path: Path) -> None:
    """Each worker collects and splits the whole suite; one writes the note, and the
    controller, which sees every report, writes what the tests took.

    The sample has a test that starts a pytest of its own, under xdist, from this
    process's environment. The note's path is an option, never the environment, so
    that pytest writes no note; one that did would overwrite the shard's with a
    collection of one test, and the check below would fail.
    """
    child = "tests/test_make_check_runs_the_suite_in_parallel.py::" + (
        "test_a_parallel_run_still_reports_a_write_outside_a_home"
    )
    for group in (1, 2):
        run = _pytest(
            "-n",
            "2",
            "--splits=2",
            f"--group={group}",
            "--splitting-algorithm=least_duration",
            "--shard-manifest",
            str(tmp_path / "leg" / f"{group}.json"),
            sample=[*_SAMPLE, child],
        )
        assert run.returncode == 0, run.stdout + run.stderr

    problems, _ = shards.check(tmp_path)
    assert problems == [], problems

    for group in (1, 2):
        ran = set(_read(tmp_path / "leg" / f"{group}.json")["selected"])
        took = _read(tmp_path / "leg" / f"{group}.durations.json")
        assert set(took) == ran, "the times are not those of the tests the shard ran"
        assert all(isinstance(seconds, float) and seconds >= 0 for seconds in took.values())

    ran = {i for group in (1, 2) for i in _read(tmp_path / "leg" / f"{group}.json")["selected"]}
    assert child in ran
    assert set(shards.durations(tmp_path, ["leg"])) == ran


# The workflow's wiring.


def test_pytest_split_is_a_dev_dependency_and_locked() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = pyproject["project"]["optional-dependencies"]["dev"]
    assert any(re.match(r"pytest-split\b", spec) for spec in dev), dev
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert '\nname = "pytest-split"\n' in lock, "uv.lock does not pin pytest-split"


def test_the_committed_durations_are_a_map_of_this_suites_tests() -> None:
    """What pytest-split splits by. A test it has no time for counts as the average,
    so the file may lag the suite; it must not be empty, or another file's shape."""
    durations = json.loads((ROOT / ".test_durations").read_text(encoding="utf-8"))
    assert isinstance(durations, dict) and len(durations) > 5000, len(durations)
    assert all(nodeid.startswith("tests/") and "::" in nodeid for nodeid in durations)
    assert all(isinstance(seconds, int | float) and seconds >= 0 for seconds in durations.values())


def test_every_shard_runs_its_part_and_keeps_its_note() -> None:
    """The whole line, not a part of it: ``pytest … || true`` keeps every part and turns a
    red shard green, and a green shard is what ``ci-ok`` passes."""
    test = workflow()["jobs"]["test"]
    commands = [
        line.strip()
        for step in test["steps"]
        for line in str(step.get("run", "")).splitlines()
        if line.strip().startswith("pytest ")
    ]
    assert len(commands) == 2, commands
    note = '--shard-manifest "${{ runner.temp }}/shards/${{ matrix.leg }}/${{ matrix.shard }}.json"'
    for command in commands:
        assert _SPLIT.search(command), f"a shard does not run its part of the leg: {command}"
        assert note in command, f"a shard does not note what it ran: {command}"
        assert command == SHARD_PYTEST, f"a shard's pytest is not the pinned line: {command}"
    for step in test["steps"]:
        script = str(step.get("run", ""))
        if "pytest " in script:
            assert "shell" not in step, (
                "a test step's shell may drop `-e`, so a red pytest is not fatal"
            )
            assert not re.search(r"^\s*set \+e\b", script, re.MULTILINE), (
                "`set +e` lets the step outlive a red pytest and end on the next command's status"
            )

    upload = [
        step
        for step in test["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    assert len(upload) == 1, "every shard uploads its note"
    assert upload[0]["if"] == "${{ !cancelled() }}", "a red shard's split must be checked too"
    assert upload[0]["with"]["path"] == "${{ runner.temp }}/shards/"
    assert upload[0]["with"]["name"] == "shard-${{ matrix.leg }}-${{ matrix.shard }}"
    assert upload[0]["with"]["if-no-files-found"] == "error"


def test_a_shards_job_name_says_its_leg_and_shard() -> None:
    name = workflow()["jobs"]["test"]["name"]
    assert name == "test (${{ matrix.leg }}, shard ${{ matrix.shard }}/${{ matrix.shards }})"


def test_windows_has_more_shards_than_a_linux_leg() -> None:
    """A test costs about 2.5 times as much on the Windows runner (ci.yml's header)."""
    counts = {(leg["os"], leg["shards"]) for leg in jobs("test", lean=False)}
    linux = {count for os_, count in counts if os_ == "ubuntu-latest"}
    windows = {count for os_, count in counts if os_ == "windows-latest"}
    assert len(linux) == 1 and len(windows) == 1, counts
    assert windows.pop() > linux.pop()


def test_the_shards_job_checks_every_shards_note() -> None:
    job = workflow()["jobs"]["shards"]
    assert job["needs"] == "test"
    assert job["if"] == "${{ !cancelled() }}", "a red shard's split must be checked too"
    download = [
        s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/download-artifact@")
    ]
    assert len(download) == 1
    assert download[0]["with"] == {
        "pattern": "shard-*",
        "path": "${{ runner.temp }}/shards",
        "merge-multiple": True,
    }
    runs = [str(s.get("run", "")) for s in job["steps"]]
    assert 'python3 -m tests.shards check "$RUNNER_TEMP/shards"' in runs


def test_every_job_has_a_timeout() -> None:
    """GitHub's default is six hours: a hung shard held a runner, and the run, that long."""
    for name, job in workflow()["jobs"].items():
        assert "timeout-minutes" in job, f"{name} has no timeout"
    test = str(workflow()["jobs"]["test"]["timeout-minutes"])
    assert test == "${{ matrix.os == 'windows-latest' && 25 || 15 }}", test


def test_ci_ok_needs_every_job_and_fails_unless_all_passed() -> None:
    """The check for branch protection: one name, whatever the legs and shards."""
    jobs_ = workflow()["jobs"]
    ok = jobs_["ci-ok"]
    assert ok["name"] == "ci-ok"
    assert set(ok["needs"]) == set(jobs_) - {"ci-ok"}, "ci-ok does not wait for every job"
    assert ok["if"] == "always()", (
        "without always() a failed job SKIPS ci-ok, and a skipped job satisfies a required check"
    )
    script = "\n".join(str(step.get("run", "")) for step in ok["steps"])
    assert 'select(.value.result != "success")' in script, "ci-ok passes some job that did not"
    assert "exit 1" in script
    assert ok["steps"][0]["env"]["NEEDS"] == "${{ toJSON(needs) }}"


def _ci_ok_script() -> str:
    steps = workflow()["jobs"]["ci-ok"]["steps"]
    assert len(steps) == 1, "ci-ok is one step, which is the one these tests run"
    return str(steps[0]["run"])


#: Every job ci-ok waits for, from ci.yml itself, so a job added later is in these cases.
_NEEDED = [str(job) for job in workflow()["jobs"]["ci-ok"]["needs"]]


def _ci_ok(results: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """ci-ok's own script, run as the runner runs it (``bash -e``), on the ``needs`` it reads."""
    needs = json.dumps({job: {"result": result, "outputs": {}} for job, result in results.items()})
    return subprocess.run(
        ["bash", "-e", "-c", _ci_ok_script()],
        env={**os.environ, "NEEDS": needs},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


_RUNS_CI_OK = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None or shutil.which("jq") is None,
    reason="runs ci-ok's own script, which is bash and jq as on ubuntu-latest",
)


@_RUNS_CI_OK
def test_ci_ok_passes_when_every_job_did() -> None:
    run = _ci_ok(dict.fromkeys(_NEEDED, "success"))
    assert run.returncode == 0, run.stdout + run.stderr


@_RUNS_CI_OK
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped"])
@pytest.mark.parametrize("job", _NEEDED)
def test_ci_ok_fails_when_any_one_job_did_not_pass(job: str, result: str) -> None:
    """Each job on its own, each way it can end short of success.

    A text pin let an inverted test (``-z`` for ``-n``), an ``exit 0`` or an extra jq
    ``select`` that drops one job through; this is the behaviour the one required check
    exists for (review of #427).
    """
    run = _ci_ok({**dict.fromkeys(_NEEDED, "success"), job: result})
    assert run.returncode != 0, f"ci-ok passed with {job} {result}: {run.stdout}"
    assert f"::error::not every job passed: {job}" in run.stdout, run.stdout


def test_no_job_or_step_continues_on_error() -> None:
    """``continue-on-error`` reports a failed step or job as a success: on any job here,
    or on ci-ok's own step, a red suite would pass the one required check."""
    for name, job in workflow()["jobs"].items():
        assert "continue-on-error" not in job, f"job {name} continues on error"
        for step in job.get("steps", []):
            assert "continue-on-error" not in step, (
                f"step {step.get('name') or step.get('uses')!r} of {name} continues on error"
            )
    text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    code = [line for line in text.splitlines() if not line.strip().startswith("#")]
    assert not [line for line in code if "continue-on-error" in line], (
        "continue-on-error is set somewhere the job and step walk above does not look"
    )


@pytest.mark.parametrize("lean", [True, False], ids=["sub-pr", "full"])
def test_every_leg_names_its_settings(lean: bool) -> None:
    """A leg with no ``ambient`` reaches the closed set and fails, rather than running
    pristine under an ambient leg's name; one with no ``shards`` would not split."""
    for leg in jobs("test", lean=lean):
        assert set(leg) == {"leg", "shard", "os", "python", "captain", "ambient", "shards"}, leg
