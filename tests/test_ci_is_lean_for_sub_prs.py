"""A pull request into ``rc/**`` runs lean CI; everything else runs the full matrix.

Owner decision 2026-10-23: shared runners give about 21 concurrent jobs and a
``check`` leg takes about 45 minutes, so a train's workstream sub-PRs queued behind
one another for hours. A sub-PR into ``rc/**`` runs lint, typecheck and the suite on
one Linux leg (py3.12, captain on), the Linux package job and the lock job; the
other legs never start a runner. A push to ``rc/**`` or ``main`` (every merge), a PR
into ``main`` (a train), the nightly and a run by hand keep the full matrix, so
nothing reaches ``main`` that the full matrix has not seen.

Read as YAML, then each ``fromJSON(<lean> && '<a>' || '<b>')`` is evaluated for both
events here: the strings are what GitHub parses, so pinning them pins the split.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
LEAN = "github.event_name == 'pull_request' && startsWith(github.base_ref, 'rc/')"
_CHOICE = re.compile(
    r"^\$\{\{\s*fromJSON\(\s*(?P<cond>.+?)\s*&&\s*'(?P<lean>[^']*)'\s*\|\|\s*'(?P<full>[^']*)'\s*\)\s*\}\}$",
    re.DOTALL,
)


def _workflow(name: str) -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(
        (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
    )
    return loaded


def _choose(value: Any, *, lean: bool) -> Any:
    """``value`` as GitHub resolves it for a sub-PR (``lean``) or any other event."""
    match = _CHOICE.match(" ".join(str(value).split()))
    assert match is not None, f"not a lean/full choice: {value!r}"
    cond = " ".join(match["cond"].split())
    assert cond == f"({LEAN})", f"the lean test is spelled differently here: {cond}"
    return json.loads(match["lean" if lean else "full"])


def _legs(matrix: dict[str, Any]) -> list[dict[str, Any]]:
    """The jobs a matrix expands to: its cross-product plus ``include``."""
    axes = {key: values for key, values in matrix.items() if key not in ("include", "exclude")}
    legs: list[dict[str, Any]] = [{}] if axes else []  # include alone is its own legs
    for key, values in axes.items():
        legs = [{**leg, key: value} for leg in legs for value in values]
    return legs + list(matrix.get("include", []))


def _skipped_on_sub_prs(job: dict[str, Any]) -> bool:
    condition = " ".join(str(job.get("if", "")).split())
    return condition in (f"${{{{ !({LEAN}) }}}}", f"!({LEAN})")


def test_a_sub_pr_runs_one_linux_check_leg() -> None:
    check = _workflow("ci.yml")["jobs"]["check"]
    assert _legs(_choose(check["strategy"]["matrix"], lean=True)) == [
        {"os": "ubuntu-latest", "python-version": "3.12", "captain": "on"}
    ]
    steps = [step.get("name", "") for step in check["steps"]]
    assert {"Lint (ruff)", "Format check (ruff)", "Type check (mypy)", "Test (pytest)"} <= set(
        steps
    )


def test_everything_else_runs_the_full_check_matrix() -> None:
    check = _workflow("ci.yml")["jobs"]["check"]
    legs = _legs(_choose(check["strategy"]["matrix"], lean=False))
    assert sorted((leg["os"], leg["python-version"], leg["captain"]) for leg in legs) == [
        ("ubuntu-latest", "3.11", "on"),
        ("ubuntu-latest", "3.12", "off"),
        ("ubuntu-latest", "3.12", "on"),
        ("ubuntu-latest", "3.13", "on"),
        ("windows-latest", "3.12", "on"),
    ]


def test_a_sub_pr_packages_on_linux_only() -> None:
    package = _workflow("ci.yml")["jobs"]["package"]
    matrix = package["strategy"]["matrix"]
    assert _choose(matrix["os"], lean=True) == ["ubuntu-latest"]
    assert _choose(matrix["os"], lean=False) == ["ubuntu-latest", "windows-latest"]


def test_a_sub_pr_skips_the_ambient_legs() -> None:
    assert _skipped_on_sub_prs(_workflow("ci.yml")["jobs"]["ambient"])


def test_every_other_job_runs_on_sub_prs() -> None:
    """The lock job, once present, and any job added later, run unless skipped here."""
    jobs = _workflow("ci.yml")["jobs"]
    assert [name for name, job in jobs.items() if _skipped_on_sub_prs(job)] == ["ambient"]


def test_ci_still_runs_on_every_event_it_did() -> None:
    triggers = _workflow("ci.yml")[True]  # PyYAML reads the bare key `on` as True
    assert triggers["pull_request"]["branches"] == ["main", "rc/**"]
    assert triggers["push"]["branches"] == ["main", "rc/**"]
    assert "schedule" in triggers and "workflow_dispatch" in triggers


def test_the_demo_renders_on_trains_and_pushes_not_sub_prs() -> None:
    triggers = _workflow("demo.yml")[True]
    assert triggers["pull_request"]["branches"] == ["main"]
    assert triggers["push"]["branches"] == ["main", "rc/**"]
