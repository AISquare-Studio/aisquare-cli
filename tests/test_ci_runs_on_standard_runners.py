"""Every job in every workflow runs on a free, standard GitHub-hosted runner.

Owner decision (2026-10-25): standard runners only, no larger ones. This repo is public,
so ``ubuntu-latest``, ``windows-latest`` and ``macos-latest`` cost nothing; a larger
label (``ubuntu-latest-4-cores``, ``windows-latest-8-cores``) or a self-hosted one bills
or queues, and one edited into ``lint`` or ``ci-ok`` would not turn a single check red.

So this reads the ``runs-on`` of EVERY job of EVERY file under ``.github/workflows`` and
holds each to the three labels the files use today. Only ``ci.yml``'s matrix-sourced
``runs-on`` (the legs' ``os``, the package job's ``os``) was pinned before, by the tests
of the matrices; a literal one was not. A ``runs-on`` that is a matrix expression
(``${{ matrix.os }}``) is resolved as GitHub resolves it, for a sub-PR and for every
other run, and every value it can take must be in the set. Anything else (an
expression of another shape, a list of labels, a ``group:``/``labels:`` mapping) is
refused rather than guessed at: widening what is accepted is an edit to the set below.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from tests.ci_matrix import ROOT, choose, expand, workflow

WORKFLOWS = ROOT / ".github" / "workflows"

#: The runners that cost nothing on a public repo, and the whole of what the files use.
STANDARD = {"ubuntu-latest", "windows-latest", "macos-latest"}

#: A ``runs-on`` that reads one key of the job's own matrix, and nothing else.
_FROM_MATRIX = re.compile(r"^\$\{\{\s*matrix\.([A-Za-z_][A-Za-z0-9_-]*)\s*\}\}$")


def _workflow_files() -> list[Path]:
    """Both extensions GitHub reads: a ``.yaml`` file runs as a ``.yml`` one does."""
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def _matrix_jobs(matrix: Any, *, lean: bool) -> list[dict[str, Any]]:
    """The jobs a job's ``strategy.matrix`` starts, with a lean/full choice resolved.

    The matrix is a ``fromJSON(<lean> && '<a>' || '<b>')`` string as a whole (``test``) or
    in the value of an axis (``package``); a plain mapping of lists is expanded as it is.
    """
    if isinstance(matrix, str):
        matrix = choose(matrix, lean=lean)
    resolved = {
        key: choose(value, lean=lean) if isinstance(value, str) else value
        for key, value in matrix.items()
    }
    return expand(resolved)


def runner_labels(job: dict[str, Any], *, lean: bool) -> list[Any]:
    """What ``job``'s ``runs-on`` can be, for a sub-PR (``lean``) or any other run."""
    value = job["runs-on"]
    named = _FROM_MATRIX.match(value) if isinstance(value, str) else None
    if named is None:
        return [value]
    matrix = job.get("strategy", {}).get("matrix")
    if matrix is None:
        return [value]  # `matrix.os` with no matrix is no label at all
    return [leg.get(named[1]) for leg in _matrix_jobs(matrix, lean=lean)] or [value]


def non_standard(name: str, doc: dict[Any, Any]) -> list[str]:
    """One line per job of ``doc`` whose ``runs-on`` is not, or can be other than, standard."""
    problems: list[str] = []
    for job_name, job in (doc.get("jobs") or {}).items():
        if "uses" in job:  # a call of a reusable workflow: it runs on the callee's runners
            if "runs-on" in job:
                problems.append(f"{name}::{job_name}: a workflow call that names a runner")
            continue
        if "runs-on" not in job:
            problems.append(f"{name}::{job_name}: no runs-on")
            continue
        for lean in (True, False):
            for label in runner_labels(job, lean=lean):
                if not (isinstance(label, str) and label in STANDARD):
                    where = "a sub-PR's" if lean else "a full"
                    problems.append(f"{name}::{job_name}: {label!r} on {where} run")
    return sorted(set(problems))


def test_every_runs_on_in_every_workflow_is_a_standard_runner() -> None:
    problems = [
        problem
        for path in _workflow_files()
        for problem in non_standard(path.name, workflow(path.name))
    ]
    assert problems == [], (
        f"only {sorted(STANDARD)} are free standard runners (owner decision 2026-10-25): "
        + "; ".join(problems)
    )


def test_the_scan_reads_every_workflow_and_the_jobs_that_gate_a_merge() -> None:
    """A rule over no job passes for any label: the files and jobs it must see are named."""
    assert {path.name for path in _workflow_files()} >= {
        "ci.yml",
        "install.yml",
        "installers-check.yml",
        "installers.yml",
        "publish.yml",
    }
    assert set(workflow("ci.yml")["jobs"]) >= {"lint", "test", "shards", "package", "ci-ok"}
    # `test` and `package` take their runner from a matrix: the legs' `os`, resolved, is
    # what the scan reads for them, not the expression's text.
    for job in ("test", "package"):
        ci = workflow("ci.yml")["jobs"][job]
        for lean in (True, False):
            labels = runner_labels(ci, lean=lean)
            assert labels and set(labels) <= STANDARD, (job, lean, labels)
    assert {*runner_labels(workflow("ci.yml")["jobs"]["test"], lean=False)} == {
        "ubuntu-latest",
        "windows-latest",
    }


def _doc(**jobs: dict[str, Any]) -> dict[Any, Any]:
    return {"jobs": jobs}


@pytest.mark.parametrize(
    "label", ["ubuntu-latest-4-cores", "windows-latest-8-cores", "macos-13-xl"]
)
def test_a_larger_label_is_refused_wherever_it_is_written(label: str) -> None:
    for job in (
        {"runs-on": label},
        {"runs-on": "${{ matrix.os }}", "strategy": {"matrix": {"os": [label]}}},
    ):
        assert non_standard("w.yml", _doc(build=job)), job


def test_one_larger_value_among_standard_ones_in_a_matrix_is_refused() -> None:
    """A leg added by ``include``, or one value of an axis, is as much a runner as a literal."""
    by_include = {
        "runs-on": "${{ matrix.os }}",
        "strategy": {
            "matrix": {
                "os": ["ubuntu-latest"],
                "include": [{"os": "ubuntu-latest-4-cores", "python": "3.12"}],
            }
        },
    }
    by_axis = {
        "runs-on": "${{ matrix.os }}",
        "strategy": {"matrix": {"os": ["ubuntu-latest", "windows-latest-8-cores"]}},
    }
    for job in (by_include, by_axis):
        assert non_standard("w.yml", _doc(build=job)), job
    ok = {
        "runs-on": "${{ matrix.os }}",
        "strategy": {"matrix": {"os": ["ubuntu-latest", "windows-latest", "macos-latest"]}},
    }
    assert non_standard("w.yml", _doc(build=ok)) == []


@pytest.mark.parametrize(
    "runs_on",
    [
        ["self-hosted", "linux"],
        ["ubuntu-latest"],
        {"group": "large-runners", "labels": "ubuntu-latest"},
        "${{ github.event_name == 'push' && 'ubuntu-latest' || 'ubuntu-latest-4-cores' }}",
        "${{ matrix.os }}",  # a matrix this job does not have
        "",
        None,
    ],
    ids=["self-hosted", "list", "group", "other-expression", "no-matrix", "empty", "null"],
)
def test_a_shape_it_cannot_vouch_for_is_refused(runs_on: Any) -> None:
    assert non_standard("w.yml", _doc(build={"runs-on": runs_on}))


def test_a_matrix_key_no_leg_has_is_refused() -> None:
    """``matrix.runner`` where the legs say ``os`` expands to nothing, and starts no job."""
    job = {"runs-on": "${{ matrix.runner }}", "strategy": {"matrix": {"os": ["ubuntu-latest"]}}}
    assert non_standard("w.yml", _doc(build=job))


def test_a_job_with_no_runner_is_refused_and_a_workflow_call_is_not() -> None:
    assert non_standard("w.yml", _doc(build={"steps": []}))
    assert non_standard("w.yml", _doc(call={"uses": "./.github/workflows/x.yml"})) == []
    assert non_standard(
        "w.yml", _doc(call={"uses": "./.github/workflows/x.yml", "runs-on": "ubuntu-latest"})
    )


def test_the_standard_set_is_the_three_free_labels() -> None:
    """Widening it is an owner decision, so it is two edits: the set and this."""
    assert {"ubuntu-latest", "windows-latest", "macos-latest"} == STANDARD
