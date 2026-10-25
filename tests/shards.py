"""What each CI shard ran, and the proof that a leg's shards ran its suite exactly once.

CI splits every leg's suite across parallel jobs with pytest-split (``--splits N
--group k``, ``.github/workflows/ci.yml``). A split that drops a test is green: nothing
fails for a test that never ran. So each shard writes down what it collected and what
it kept (``--shard-manifest``), and CI's ``shards`` job reads every shard's note and
fails unless, for each leg, every shard is there, all of them collected the same suite,
and their selections add up to that suite exactly: each collected test in one shard,
none in two, nothing that was not collected.

The note is written from collection itself (a wrapper around
``pytest_collection_modifyitems``), not from what pytest-split reports as deselected:
the split is what is being checked, so it does not get to say what the suite was.

Each shard also writes what its tests took beside the note (``<group>.durations.json``),
so the times pytest-split splits by can come from CI's own runners:
``python -m tests.shards durations <dir> <leg>...`` turns a run's notes into a
``.test_durations`` (``gh run download <run> -p 'shard-*' -D <dir>`` fetches them).

Stdlib only at import: the ``shards`` job runs ``python -m tests.shards check <dir>`` on
a runner with nothing installed, and the plugin imports pytest only when it is built.
"""

from __future__ import annotations

import json
import os
import platform
import re
import sys
from collections import Counter
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

OPTION = "--shard-manifest"
"""Where a shard writes its note: ``<dir>/<leg>/<group>.json``. A command-line option and
never an environment variable, so a pytest a test starts in a subprocess (several do)
does not overwrite the shard's note with its own collection."""

#: How many ids a failure lists per kind before it says how many more there are.
_SHOWN = 10

#: A shard's note, ``<group>.json``; its times are ``<group>.durations.json`` beside it.
_NOTE = re.compile(r"^(\d+)\.json$")


def add_option(parser: pytest.Parser) -> None:
    parser.addoption(
        OPTION,
        metavar="PATH",
        default=None,
        help="CI: write what this shard collected and selected to PATH (tests/shards.py)",
    )


def _worker() -> bool:
    return "PYTEST_XDIST_WORKER" in os.environ


def plugin(path: Path) -> object:
    """The plugin that writes ``path`` once collection, and the split, are done.

    And, at the end of the run, what each test took (setup, call and teardown, as
    pytest-split's ``--store-durations`` counts it), from the process that sees every
    report: xdist's controller, which hands no test to itself, or a run without xdist.
    """
    import pytest

    class Manifest:
        def __init__(self) -> None:
            self.took: dict[str, float] = {}

        def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
            if not _worker():
                self.took[report.nodeid] = self.took.get(report.nodeid, 0.0) + report.duration

        def pytest_sessionfinish(self) -> None:
            if not _worker():
                durations = path.with_name(f"{path.stem}.durations.json")
                durations.parent.mkdir(parents=True, exist_ok=True)
                durations.write_text(json.dumps(self.took, sort_keys=True), encoding="utf-8")

        @pytest.hookimpl(wrapper=True)
        def pytest_collection_modifyitems(
            self, config: pytest.Config, items: list[pytest.Item]
        ) -> Iterator[None]:
            collected = [item.nodeid for item in items]
            yield
            # Every xdist worker collects the whole suite and splits it the same way;
            # one of them writes. No worker at all (`-n 0`) is the same run, unsplit.
            if os.environ.get("PYTEST_XDIST_WORKER", "gw0") == "gw0":
                write(
                    path,
                    splits=config.getoption("splits", None),
                    group=config.getoption("group", None),
                    collected=collected,
                    selected=[item.nodeid for item in items],
                )

    return Manifest()


def write(
    path: Path,
    *,
    splits: int | None,
    group: int | None,
    collected: Sequence[str],
    selected: Sequence[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    note = {
        "splits": splits,
        "group": group,
        "python": platform.python_version(),
        "platform": sys.platform,
        "collected": list(collected),
        "selected": list(selected),
    }
    path.write_text(json.dumps(note), encoding="utf-8")


def _listed(kind: str, ids: Sequence[str]) -> str:
    more = f" … and {len(ids) - _SHOWN} more" if len(ids) > _SHOWN else ""
    return f"{kind} ({len(ids)}): " + ", ".join(ids[:_SHOWN]) + more


def check_leg(leg: str, notes: dict[int, dict[str, Any]]) -> tuple[list[str], str]:
    """Problems with one leg's shards (``notes`` by group), and a line saying what held."""
    problems: list[str] = []
    splits = {note.get("splits") for note in notes.values()}
    count = next(iter(splits))
    if len(splits) != 1 or not isinstance(count, int):
        return [f"{leg}: the shards disagree on, or omit, how many there are: {splits}"], ""
    for group, note in notes.items():
        if note.get("group") != group:
            problems.append(f"{leg}: {group}.json is the note of group {note.get('group')}")
    missing_shards = sorted(set(range(1, count + 1)) - set(notes))
    if missing_shards:
        problems.append(f"{leg}: no note from shard(s) {missing_shards} of {count}")
    extra_shards = sorted(set(notes) - set(range(1, count + 1)))
    if extra_shards:
        problems.append(f"{leg}: a note from shard(s) {extra_shards}, past {count}")

    suites = {tuple(note["collected"]) for note in notes.values()}
    if len(suites) != 1:
        collected = {group: len(note["collected"]) for group, note in sorted(notes.items())}
        problems.append(
            f"{leg}: the shards collected different suites (tests per shard: {collected}), so a "
            "split of one is not a split of the others"
        )
    if problems:
        return problems, ""

    suite = Counter(next(iter(suites)))
    ran = Counter(nodeid for note in notes.values() for nodeid in note["selected"])
    never = sorted((suite - ran).elements())
    twice = sorted(nodeid for nodeid, times in ran.items() if times > 1)
    foreign = sorted(set(ran) - set(suite))
    if never:
        problems.append(f"{leg}: " + _listed("collected, and run by no shard", never))
    if twice:
        problems.append(f"{leg}: " + _listed("run by more than one shard", twice))
    if foreign:
        problems.append(f"{leg}: " + _listed("run, and never collected", foreign))
    if problems:
        return problems, ""
    sizes = ", ".join(str(len(notes[group]["selected"])) for group in sorted(notes))
    return [], (
        f"{leg}: {count} shards ran the {sum(suite.values())} collected tests, each exactly "
        f"once ({sizes})"
    )


def check(root: Path) -> tuple[list[str], list[str]]:
    """Every leg's notes under ``root`` (``<leg>/<group>.json``): (problems, what held)."""
    legs: dict[str, dict[int, dict[str, Any]]] = {}
    problems: list[str] = []
    for path in sorted(root.glob("*/*.json")):
        found = _NOTE.match(path.name)
        if found is None:
            continue  # a shard's times, read by `durations`
        group = int(found.group(1))
        try:
            note = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as error:
            problems.append(f"{path.parent.name}/{path.name}: unreadable ({error})")
            continue
        legs.setdefault(path.parent.name, {})[group] = note
    if not legs and not problems:
        problems.append(f"no shard notes under {root}: nothing proves any split")
    held: list[str] = []
    for leg, notes in sorted(legs.items()):
        wrong, line = check_leg(leg, notes)
        problems += wrong
        if line:
            held.append(line)
    return problems, held


def durations(root: Path, legs: Sequence[str]) -> dict[str, float]:
    """What each test took in the shards of ``legs`` under ``root``, the mean of the legs.

    Rounded to the millisecond: the split needs no finer, and the committed file
    stays a third of the size.
    """
    took: dict[str, list[float]] = {}
    for leg in legs:
        times = sorted((root / leg).glob("*.durations.json"))
        if not times:
            raise SystemExit(f"no shard times for {leg} under {root}")
        for path in times:
            for nodeid, seconds in json.loads(path.read_text(encoding="utf-8")).items():
                took.setdefault(nodeid, []).append(seconds)
    return {nodeid: round(sum(each) / len(each), 3) for nodeid, each in sorted(took.items())}


_USAGE = """usage:
  python -m tests.shards check <dir>             every leg's shards ran its suite once
  python -m tests.shards durations <dir> <leg>…  those legs' times, as a .test_durations
<dir> holds a run's notes as <leg>/<group>.json (and <group>.durations.json)"""


def main(argv: Sequence[str]) -> int:
    if len(argv) >= 3 and argv[0] == "durations":
        print(json.dumps(durations(Path(argv[1]), argv[2:]), indent=4, sort_keys=True))
        return 0
    if len(argv) != 2 or argv[0] != "check":
        print(_USAGE)
        return 2
    problems, held = check(Path(argv[1]))
    for line in held:
        print(f"shards complete and disjoint: {line}")
    for problem in problems:
        print(f"::error::{problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
