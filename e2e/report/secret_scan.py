"""A fail-closed scan for credentials in a run directory, before any of it is made public.

    python -m e2e.report.secret_scan <run_dir>        exit 0 clean · 3 on a hit · 2 no such dir

PLAN.md §10: everything in an artifact of a public repository is public, so the
run is scanned before it leaves the runner, and :mod:`e2e.report.publish` will
not publish a run this finds anything in. This is the high-precision core of
that scan: token shapes that never belong on a test machine's screen, so a hit
is almost certainly real. PLAN.md lists what is still to add (the product's own
redaction patterns, emails outside ``example.invalid``, high entropy); until
then a miss is possible.

What is scanned is every file of the run except pictures, video and report/:
run.json holds every string the gallery and summaries print, text/ holds every
screen the PNGs show (pixels themselves cannot be scanned), and logs/ ships in
the same artifact. report/ is derived from those, and its gallery is mostly
base64, where a token shape can occur by chance.

A hit names the file, the byte offset and the rule, never the match: the
scan's own output lands in a public log.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

RULES: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("anthropic-key", re.compile(rb"sk-ant-[A-Za-z0-9_-]{8,}")),
    ("github-fine-grained-token", re.compile(rb"github_pat_[A-Za-z0-9_]{20,}")),
    ("github-token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("slack-webhook", re.compile(rb"hooks\.slack\.com/services/[A-Za-z0-9/_-]{8,}")),
    ("slack-token", re.compile(rb"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("aws-access-key", re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private-key", re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)
SKIPPED_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".webm", ".mp4", ".zip"})


@dataclass(frozen=True)
class Hit:
    path: str
    """Relative to the run directory."""
    offset: int
    rule: str

    def __str__(self) -> str:
        return f"{self.path}:{self.offset}: {self.rule}"


def scanned_files(run_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in run_dir.rglob("*")
        if path.is_file()
        and path.suffix.lower() not in SKIPPED_SUFFIXES
        and path.relative_to(run_dir).parts[0] != "report"
    )


def scan(run_dir: Path) -> list[Hit]:
    hits: list[Hit] = []
    for path in scanned_files(run_dir):
        data = path.read_bytes()
        relative = path.relative_to(run_dir).as_posix()
        for name, rule in RULES:
            hits.extend(Hit(relative, match.start(), name) for match in rule.finditer(data))
    return sorted(hits, key=lambda hit: (hit.path, hit.offset))


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0].startswith("-"):
        print("usage: python -m e2e.report.secret_scan <run_dir>", file=sys.stderr)
        return 2
    run_dir = Path(args[0])
    if not run_dir.is_dir():
        print(f"secret_scan: {run_dir} is not a directory", file=sys.stderr)
        return 2
    files = scanned_files(run_dir)
    hits = scan(run_dir)
    for hit in hits:
        print(f"secret_scan: {hit}", file=sys.stderr)
    if hits:
        print(
            f"secret_scan: {len(hits)} credential-shaped string(s); do not publish", file=sys.stderr
        )
        return 3
    print(f"secret_scan: {len(files)} files clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
