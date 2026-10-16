"""``python -m e2e.report.build <run_dir> [--image-base URL]``: run.json into report/.

Reads ``<run_dir>/run.json`` (``e2e.contracts.load``) and writes ``<run_dir>/report/``:

- ``gallery.html``: one self-contained page, every screenshot inlined (:mod:`.gallery`);
- ``summary.md``: the issue / PR-comment body, ``<img>`` tags under ``--image-base``
  or relative to report/ without one (:mod:`.markdown`);
- ``step-summary.md``: the same without pictures, for ``$GITHUB_STEP_SUMMARY``;
- ``verdict.json``: the release gate's shape (:mod:`.gate`).

The artifact named in the summaries comes from ``E2E_ARTIFACT_NAME`` (default
``e2e-report``); ``GITHUB_REPOSITORY`` / ``GITHUB_RUN_ID``, when a workflow sets
them, turn the commit and the run into links.

Exit status: **0 whenever the report was written, whatever the verdict** — the
verdict is data, and the workflow step that gates reads verdict.json; **2 when
run.json is missing or cannot be trusted** (not JSON, another schema, a status
outside the contract). On 2 any report an earlier build left in report/ is
removed, so a stale verdict.json cannot stand in for this run's.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from e2e.contracts import Run, load
from e2e.report import gallery, gate, markdown
from e2e.report.facts import Facts, Links, duration, of, problems

OUTPUTS = ("gallery.html", "summary.md", "step-summary.md", "verdict.json")


class InvalidRun(Exception):
    """run.json is missing, unreadable, or says something outside the contract."""


@dataclass(frozen=True)
class Built:
    facts: Facts
    report_dir: Path
    page: gallery.Page

    def path(self, name: str) -> Path:
        return self.report_dir / name


def read_run(run_dir: Path) -> Run:
    path = run_dir / "run.json"
    if not path.is_file():
        raise InvalidRun(f"{path}: missing")
    try:
        run = load(path)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # ValueError covers bad JSON and a wrong schema; KeyError / TypeError a
        # missing or unexpected field; AttributeError a document that is not an object.
        raise InvalidRun(f"{path}: {type(exc).__name__}: {exc}") from exc
    found = problems(run)
    if found:
        raise InvalidRun(f"{path}: " + "; ".join(found))
    return run


def build(run_dir: Path, *, image_base: str | None = None, links: Links | None = None) -> Built:
    """Write the four outputs for ``run_dir``; raises ``InvalidRun`` instead of guessing."""
    links = links or Links()
    facts = of(read_run(run_dir))
    report_dir = run_dir / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    page = gallery.render(facts, run_dir, links)
    _write(report_dir / "gallery.html", page.html)
    _write(
        report_dir / "summary.md",
        markdown.summary(facts, image_base=image_base, links=links),
    )
    _write(report_dir / "step-summary.md", markdown.step_summary(facts, links=links))
    verdict = json.dumps(gate.payload(facts), indent=2, ensure_ascii=False) + "\n"
    _write(report_dir / "verdict.json", verdict)
    return Built(facts, report_dir, page)


def write_summary(run_dir: Path, *, image_base: str | None, links: Links | None = None) -> Path:
    """Rewrite only summary.md, e.g. once the publisher knows where the shots are served."""
    facts = of(read_run(run_dir))
    path = run_dir / "report" / "summary.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    _write(path, markdown.summary(facts, image_base=image_base, links=links or Links()))
    return path


def _write(path: Path, text: str) -> None:
    # A sibling temp file and a rename: a reader (the publisher, a workflow step) never
    # sees half a verdict.json.
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def _remove_stale(run_dir: Path) -> None:
    for name in OUTPUTS:
        (run_dir / "report" / name).unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m e2e.report.build",
        description="Build gallery.html, summary.md, step-summary.md and verdict.json "
        "from <run_dir>/run.json into <run_dir>/report/.",
    )
    parser.add_argument("run_dir", type=Path, help="a run directory holding run.json")
    parser.add_argument(
        "--image-base",
        metavar="URL",
        help="where the run's shots/ is served from, for summary.md's <img> tags "
        "(default: paths relative to report/, for local viewing)",
    )
    args = parser.parse_args(argv)
    try:
        built = build(args.run_dir, image_base=args.image_base, links=Links.from_env(os.environ))
    except InvalidRun as exc:
        _remove_stale(args.run_dir)
        print(f"e2e.report.build: {exc}", file=sys.stderr)
        return 2
    facts = built.facts
    counts = facts.counts
    print(
        f"e2e report: {facts.verdict} · {counts.passed}/{counts.total} steps passed · "
        f"{facts.journeys_passed}/{len(facts.journeys)} journeys passed · "
        f"{duration(facts.duration_s)}"
    )
    page = built.page
    summary = built.path("summary.md").read_text(encoding="utf-8")
    print(
        f"  {built.path('gallery.html')}  "
        f"({page.shots_inlined} screenshots inlined, {len(page.html):,} bytes)"
    )
    print(f"  {built.path('summary.md')}  ({markdown.size(summary):,} chars)")
    print(f"  {built.path('step-summary.md')}")
    print(f"  {built.path('verdict.json')}  ({len(facts.failures)} failure(s))")
    if page.missing:
        print(
            f"  warning: {len(page.missing)} file(s) named in run.json are missing", file=sys.stderr
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
