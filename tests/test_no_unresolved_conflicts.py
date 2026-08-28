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
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    paths = [REPO / n for n in out.split("\0") if n]
    return [p for p in paths if p.suffix in {".py", ".md", ".toml", ".cfg", ".yml", ".yaml"}]


def _markers_in(text: str, where: str) -> list[str]:
    """Lines of `text` that begin a conflict marker.

    THE RULE, extracted so a control can hand it text that MUST be reported.
    Blinding it — `if False and any(...)` — left this file's two tests passing:
    the guard reported every tracked file clean while matching nothing. Its
    scanner was already controlled by `test_the_sweep_is_looking_at_something`,
    which is the half-controlled shape @9bbc8ed7 has now found six times:
    "that control proves the SCANNER SEES FILES, not that the RULE CONSULTS IT."

    The failure this prevents is the one that motivated the whole file — markers
    in a docstring parse, pass lint, and pass the gate — so a guard that reports
    clean while inspecting nothing restores exactly the hole it was built for.
    """
    return [
        f"{where}:{number}: {line[:40]}"
        for number, line in enumerate(text.splitlines(), start=1)
        if any(line.startswith(marker) for marker in MARKERS)
    ]


def test_no_tracked_file_contains_a_conflict_marker() -> None:
    offenders: list[str] = []
    for path in _tracked_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        offenders.extend(_markers_in(text, str(path.relative_to(REPO))))
    assert not offenders, "unresolved merge conflict in tracked files:\n" + "\n".join(offenders)


def test_the_sweep_is_looking_at_something() -> None:
    """Guard the guard: an empty file list would satisfy the check vacuously."""
    files = _tracked_text_files()
    assert len(files) > 100, (
        f"only {len(files)} text files swept — the sweep is broken, not the tree"
    )
    assert any(p.suffix == ".md" for p in files), (
        "no markdown swept; docstring conflicts are not the only kind"
    )


#: A conflict as git writes one, assembled from MARKERS so this file still does
#: not contain a literal marker and still cannot trip its own check.
_CONFLICT = "\n".join(
    [
        "def f():",
        f"{MARKERS[0]} HEAD",
        '    return "ours"',
        MARKERS[1],
        '    return "theirs"',
        f"{MARKERS[2]} branch",
    ]
)


def test_the_rule_still_reports_a_real_conflict() -> None:
    """POSITIVE control on the RULE, which is what was missing.

    Driven with text that MUST be reported, so a rule that has stopped matching
    fails here rather than certifying the tree. Each marker separately, because
    a rule that caught only the opening one would still miss the halves git
    leaves behind after a partial resolution.
    """
    found = _markers_in(_CONFLICT, "synthetic.py")

    assert len(found) == 3, f"the rule reported {len(found)} of 3 markers: {found}"
    for marker in MARKERS:
        assert any(marker in line for line in found), f"the rule no longer reports {marker!r}"


def test_the_rule_leaves_ordinary_text_alone() -> None:
    """NEGATIVE control, so "report every line" is not a fix.

    Includes the shapes closest to a marker — a markdown heading, a diff line,
    a docstring rule — because a guard that flagged those would fire on this
    repo's own documentation and be deleted rather than repaired.
    """
    ordinary = "\n".join(
        [
            "# a heading",
            "## a smaller heading",
            "--- a horizontal rule",
            "+++ b/file.py",
            "    normal code",
            "===== not a marker, just punctuation",
        ]
    )

    assert _markers_in(ordinary, "synthetic.py") == []
