"""No tracked file may contain merge-conflict markers.

Found the hard way at cycle 91. A conflict inside a Python DOCSTRING is valid
Python text: the module parses, its tests pass, and `make check` reported EXIT 0
with 1540 passed while `git status` showed `UU`. The planner's fold flow is
merge -> gate -> push if green, and nothing in it reads git status, so a green
gate on an unresolved merge was one commit away from putting conflict markers
into an operator-facing docstring on the train.

The gate could not see it because the failure was not a failure of behaviour.
This is the cheapest possible check for it and it belongs in the suite rather
than in a habit, because habits are what the gate exists to replace.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: Git's own markers, spelled so this file does not trip its own check.
MARKERS = ("<" * 7, "=" * 7, ">" * 7)


def _tracked_text_files() -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"],
        capture_output=True, text=True, check=True,
    ).stdout
    paths = [REPO / n for n in out.split("\0") if n]
    return [p for p in paths if p.suffix in {".py", ".md", ".toml", ".cfg", ".yml", ".yaml"}]


def test_no_tracked_file_contains_a_conflict_marker() -> None:
    offenders: list[str] = []
    for path in _tracked_text_files():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        for number, line in enumerate(lines, start=1):
            if any(line.startswith(marker) for marker in MARKERS):
                offenders.append(f"{path.relative_to(REPO)}:{number}: {line[:40]}")
    assert not offenders, "unresolved merge conflict in tracked files:\n" + "\n".join(offenders)


def test_the_sweep_is_looking_at_something() -> None:
    """Guard the guard: an empty file list would satisfy the check vacuously."""
    files = _tracked_text_files()
    assert len(files) > 100, f"only {len(files)} text files swept — the sweep is broken, not the tree"
    assert any(p.suffix == ".md" for p in files), "no markdown swept; docstring conflicts are not the only kind"
