"""A pull request into ``rc/**`` runs lean CI; everything else runs the full matrix.

Owner decision 2026-10-23: shared runners give about 21 concurrent jobs and a
``check`` leg took about 45 minutes, so a train's workstream sub-PRs queued behind
one another for hours. A sub-PR into ``rc/**`` runs lint, the py3.12 leg (captain on,
no ambient) in its shards, the shard check, the Linux package job and ``ci-ok``; the
other legs never start a runner. A push to ``rc/**`` or ``main`` (every merge), a PR
into ``main`` (a train), the nightly and a run by hand keep the full matrix, so
nothing reaches ``main`` that the full matrix has not seen.

Read as YAML, then each ``fromJSON(<lean> && '<a>' || '<b>')`` is evaluated for both
events and expanded as GitHub expands a matrix (``tests/ci_matrix.py``): the strings
are what GitHub parses, so pinning them pins the split.
"""

from __future__ import annotations

from typing import Any

from tests.ci_matrix import LEAN, choose, jobs, workflow

#: Each leg of the full matrix: (os, python, captain, ambient, shards).
FULL_LEGS = {
    "py3.11-ambient-proxy-down": ("ubuntu-latest", "3.11", "on", "proxy-down", 4),
    "py3.12": ("ubuntu-latest", "3.12", "on", "none", 4),
    "py3.13-ambient-proxy-up": ("ubuntu-latest", "3.13", "on", "proxy-up", 4),
    "py3.12-captain-off": ("ubuntu-latest", "3.12", "off", "none", 4),
    "windows-py3.12": ("windows-latest", "3.12", "on", "none", 6),
}

#: What only the ``lint`` job runs, once, rather than every leg.
LINT_STEPS = {
    "Lint (ruff)",
    "Format check (ruff)",
    "Type check (mypy)",
    "Type check as Windows (mypy --platform win32)",
    "Check the lock",
}


def _legs(matrix_jobs: list[dict[str, Any]]) -> dict[str, tuple[Any, ...]]:
    """Each leg's settings, asserting every job of the leg agrees on them."""
    legs: dict[str, tuple[Any, ...]] = {}
    for job in matrix_jobs:
        settings = tuple(job[k] for k in ("os", "python", "captain", "ambient", "shards"))
        assert legs.setdefault(job["leg"], settings) == settings, job
    return legs


def _shards(matrix_jobs: list[dict[str, Any]]) -> dict[str, list[int]]:
    shards: dict[str, list[int]] = {}
    for job in matrix_jobs:
        shards.setdefault(job["leg"], []).append(job["shard"])
    return {leg: sorted(numbers) for leg, numbers in shards.items()}


def _skipped_on_sub_prs(job: dict[str, Any]) -> bool:
    condition = " ".join(str(job.get("if", "")).split())
    return condition in (f"${{{{ !({LEAN}) }}}}", f"!({LEAN})")


def test_a_sub_pr_runs_one_linux_leg_in_its_shards() -> None:
    lean = jobs("test", lean=True)
    assert _legs(lean) == {"py3.12": ("ubuntu-latest", "3.12", "on", "none", 4)}
    assert _shards(lean) == {"py3.12": [1, 2, 3, 4]}
    steps = {step.get("name", "") for step in workflow()["jobs"]["test"]["steps"]}
    assert "Test (pytest)" in steps


def test_everything_else_runs_the_full_matrix() -> None:
    full = jobs("test", lean=False)
    assert _legs(full) == FULL_LEGS
    assert _shards(full) == {
        leg: list(range(1, shards + 1)) for leg, (*_, shards) in FULL_LEGS.items()
    }, "a leg runs some shard twice, or not at all"


def test_lint_runs_once_and_on_no_leg() -> None:
    """ruff, the format check, mypy and the lock ran on every leg; once is the same check."""
    steps = workflow()["jobs"]
    lint = {step.get("name", "") for step in steps["lint"]["steps"]}
    assert lint >= LINT_STEPS
    assert "strategy" not in steps["lint"], "lint is one job, not a matrix"
    leg = {step.get("name", "") for step in steps["test"]["steps"]}
    assert not leg & LINT_STEPS, f"a test leg still runs {leg & LINT_STEPS}"


def test_a_sub_pr_packages_on_linux_only() -> None:
    package = workflow()["jobs"]["package"]
    matrix = package["strategy"]["matrix"]
    assert choose(matrix["os"], lean=True) == ["ubuntu-latest"]
    assert choose(matrix["os"], lean=False) == ["ubuntu-latest", "windows-latest"]


def test_a_sub_pr_skips_the_ambient_legs() -> None:
    """Its merge runs both on ``rc/**``; a sub-PR's one leg runs in no ambient."""
    assert {job["ambient"] for job in jobs("test", lean=True)} == {"none"}
    assert {job["ambient"] for job in jobs("test", lean=False)} >= {"proxy-down", "proxy-up"}


def test_every_job_runs_on_sub_prs() -> None:
    """A sub-PR's lean set is chosen by the matrices, so no job is skipped for it whole.

    ``ci-ok`` needs every job, and a job skipped for a sub-PR would have to be told
    apart from one skipped because something broke.
    """
    assert [name for name, job in workflow()["jobs"].items() if _skipped_on_sub_prs(job)] == []


def test_ci_still_runs_on_every_event_it_did() -> None:
    triggers = workflow()[True]  # PyYAML reads the bare key `on` as True
    assert triggers["pull_request"]["branches"] == ["main", "rc/**"]
    assert triggers["push"]["branches"] == ["main", "rc/**"]
    assert "schedule" in triggers and "workflow_dispatch" in triggers


def test_the_demo_renders_on_trains_and_pushes_not_sub_prs() -> None:
    triggers = workflow("demo.yml")[True]
    assert triggers["pull_request"]["branches"] == ["main"]
    assert triggers["push"]["branches"] == ["main", "rc/**"]
