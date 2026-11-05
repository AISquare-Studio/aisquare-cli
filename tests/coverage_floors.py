"""Combine the py3.12 leg's coverage shards, report Claude Code's path and the seam, hold floors.

``python -m tests.coverage_floors <dir> --shards N`` (ci.yml's ``coverage`` job): reads the
``<k>.coverage`` file each of the leg's ``N`` shards left beside its note in ``<dir>`` (a
missing one fails: a floor read off part of the suite would be a lie), combines them, and
prints line and
branch coverage for every module of ``core/agent_adapters`` and the shared seam
(:data:`SEAM`), as a Markdown table, to stdout and to ``$GITHUB_STEP_SUMMARY``. It fails when
a module of Claude Code's adapter (``core/agent_adapters/claude_*.py``) or of the seam reads
under its floor (:data:`FLOORS`): what CI measured when the floor was set, less one point,
so coverage cannot drop without a commit that lowers the floor and says why.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

PACKAGE = "src/aisquare"
ADAPTERS = f"{PACKAGE}/core/agent_adapters"
#: The shared code every agent's launch, hooks, fleet and accounts run through.
SEAM = (
    f"{PACKAGE}/services/fleet.py",
    f"{PACKAGE}/services/agent_launch.py",
    f"{PACKAGE}/services/handoff.py",
    f"{PACKAGE}/core/agents.py",
    f"{PACKAGE}/core/launch_seal.py",
    f"{PACKAGE}/services/claude_accounts.py",
    f"{PACKAGE}/core/claude_accounts.py",
    f"{PACKAGE}/services/store_broker.py",
)
#: Line and branch coverage (percent) each floored module may not read under: CI's figure on
#: the commit that set it, less one point.
FLOORS: Mapping[str, tuple[float, float]] = {}


@dataclass(frozen=True)
class Figures:
    path: str
    statements: int
    covered: int
    branches: int
    covered_branches: int

    @property
    def line(self) -> float:
        return 100.0 * self.covered / self.statements if self.statements else 100.0

    @property
    def branch(self) -> float:
        return 100.0 * self.covered_branches / self.branches if self.branches else 100.0


def floored(path: str) -> bool:
    """Whether ``path`` is held to a floor: Claude Code's adapter modules and the seam."""
    name = path.rpartition("/")[2]
    in_adapters = path.rpartition("/")[0] == ADAPTERS
    return (in_adapters and name.startswith("claude_")) or path in SEAM


def reported(path: str) -> bool:
    return path.rpartition("/")[0] == ADAPTERS or path in SEAM


def figures(report: Mapping[str, object]) -> dict[str, Figures]:
    """Each reported module's figures, from ``coverage json``'s report."""
    files = report.get("files")
    assert isinstance(files, dict), "not a coverage json report"
    found: dict[str, Figures] = {}
    for path, entry in files.items():
        if not reported(path):
            continue
        summary = entry["summary"]
        found[path] = Figures(
            path,
            summary["num_statements"],
            summary["covered_lines"],
            summary.get("num_branches", 0),
            summary.get("covered_branches", 0),
        )
    return found


def problems(found: Mapping[str, Figures], floors: Mapping[str, tuple[float, float]]) -> list[str]:
    """Why the coverage fails, one line each: a floored module missing, or under its floor."""
    out: list[str] = []
    for path in sorted(p for p in (*found, *floors, *SEAM) if floored(p)):
        if path not in found:
            out.append(f"{path}: not measured at all")
            continue
        line, branch = floors.get(path, (0.0, 0.0))
        got = found[path]
        if got.line < line:
            out.append(f"{path}: line coverage {got.line:.1f}% is under its floor {line:.1f}%")
        if got.branch < branch:
            out.append(
                f"{path}: branch coverage {got.branch:.1f}% is under its floor {branch:.1f}%"
            )
    return out


def regions(report: Mapping[str, object], top: int = 10) -> list[tuple[str, int]]:
    """The ``top`` largest runs of statements no test ran in Claude Code's adapter modules
    (``core/agent_adapters/claude_*.py``), each as ``<module>:<first>-<last>`` and its size: a
    run ends at a statement that ran."""
    files = report.get("files")
    assert isinstance(files, dict), "not a coverage json report"
    found: list[tuple[str, int]] = []
    for path, entry in files.items():
        if path.rpartition("/")[0] != ADAPTERS or not path.rpartition("/")[2].startswith("claude_"):
            continue
        missing = set(entry["missing_lines"])
        run: list[int] = []
        for line in sorted(missing | set(entry["executed_lines"])):
            if line in missing:
                run.append(line)
                continue
            if run:
                found.append((f"{path.removeprefix(PACKAGE + '/')}:{run[0]}-{run[-1]}", len(run)))
            run = []
        if run:
            found.append((f"{path.removeprefix(PACKAGE + '/')}:{run[0]}-{run[-1]}", len(run)))
    return sorted(found, key=lambda item: (-item[1], item[0]))[:top]


def table(found: Mapping[str, Figures], floors: Mapping[str, tuple[float, float]]) -> str:
    rows = [
        "| module | lines | line % | branches | branch % | floor (line / branch) |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for path in sorted(found):
        got = found[path]
        floor = floors.get(path)
        said = f"{floor[0]:.1f} / {floor[1]:.1f}" if floor else ("unset" if floored(path) else "")
        rows.append(
            f"| `{path.removeprefix(PACKAGE + '/')}` | {got.covered}/{got.statements} | "
            f"{got.line:.1f} | {got.covered_branches}/{got.branches} | {got.branch:.1f} | {said} |"
        )
    return "\n".join(rows)


def combine(directory: Path, shards: int, out: Path) -> dict[str, object]:
    """Combine the shards' data files in ``directory`` into ``out``; its json report."""
    from coverage import Coverage

    wanted = [directory / f"{shard}.coverage" for shard in range(1, shards + 1)]
    missing = [path.name for path in wanted if not path.is_file()]
    if missing:
        raise SystemExit(f"coverage: no data from {', '.join(missing)}: the leg did not finish")
    coverage = Coverage(data_file=str(out))
    coverage.combine([str(path) for path in wanted], keep=True)
    coverage.save()
    report = out.with_suffix(".json")
    coverage.json_report(outfile=str(report))
    data: dict[str, object] = json.loads(report.read_text(encoding="utf-8"))
    return data


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.coverage_floors")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--shards", type=int, required=True)
    asked = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="asq-coverage-") as scratch:
        report = combine(asked.directory, asked.shards, Path(scratch) / ".coverage")
    found = figures(report)
    shown = table(found, FLOORS)
    failed = problems(found, FLOORS)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    parts = [
        "## Coverage: Claude Code's adapter and the shared seam (py3.12, every shard)",
        shown,
        "### The largest uncovered regions of Claude Code's adapter\n\n"
        + "\n".join(f"- `{where}`: {size} statements" for where, size in regions(report)),
    ]
    if failed:
        parts.append("### Under a floor\n\n" + "\n".join(f"- {line}" for line in failed))
    text = "\n\n".join(parts)
    print(text)
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    for line in failed:
        print(f"::error::{line}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
