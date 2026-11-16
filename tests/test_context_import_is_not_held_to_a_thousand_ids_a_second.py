"""``aisquare context import`` is not slowed by the bound on an id's lead (r26 nit 2 of #113, task
#35 reopened).

``context import`` mints one id per entry with a ~0.15 ms write, which is faster than the
millisecond the ids are ordered by, so a bulk import runs ahead of the clock. An id that waited
for the clock past 50 ms of lead held it to a thousand entries a second: 1500 entries took 0.19 s
before the bound and 1.46 s with it. Past the bound an id now stays in the millisecond of the one
before it and takes a larger tail, and nothing waits.

The test is a ratio against the same import with the bound lifted, which is what the import did
before the bound: an absolute time says what the machine is, not what the ids do.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from aisquare.core import ids
from aisquare.services import context as context_service

ENTRIES = 1_500
ROUNDS = 3

#: What the import is allowed to cost over the unbounded one. A wait of the bound is 8 times; the
#: bound's own work is a few microseconds an id against a write of 150.
ALLOWED_RATIO = 2.0


def test_importing_fifteen_hundred_entries_costs_what_it_does_without_the_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    source = tmp_path / "bulk.json"
    source.write_text(
        json.dumps([{"text": f"entry {n}", "pool": "user"} for n in range(ENTRIES)]),
        encoding="utf-8",
    )
    bound = ids._MAX_LEAD_MS
    found = ids._LAST_MS[0]

    def seconds_with(lead_bound: int) -> float:
        """One import, from a process whose ids have run ahead of nothing."""
        monkeypatch.setattr(ids, "_MAX_LEAD_MS", lead_bound)
        ids._LAST_MS[0] = 0
        started = time.perf_counter()
        assert context_service.import_entries(source) == ENTRIES
        return time.perf_counter() - started

    unbounded: list[float] = []
    bounded: list[float] = []
    try:
        for _round in range(ROUNDS):  # alternating, so a slow moment of the machine is both's
            unbounded.append(seconds_with(10**9))
            bounded.append(seconds_with(bound))
    finally:
        ids._LAST_MS[0] = found

    assert min(bounded) < ALLOWED_RATIO * min(unbounded), (
        f"an import with the bound took {min(bounded):.3f} s, without it {min(unbounded):.3f} s"
    )
