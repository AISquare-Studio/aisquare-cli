"""``ledger.recorded_since`` and the two ``compacted_since`` readers built on it (#17, pinned by
the tester's mutations in #24).

Asked every few seconds for as long as a session lives, so it reads the end of the transcript
and goes further back only while that end does not yet reach ``since``: widened four times
over, up to the whole file. Two things must hold, and no test held either: a record far
back IS found (a compaction followed by 700 KB of work), and a record the first look settles
costs no more than the first look.
"""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import ledger, transcripts
from aisquare.core.agent_adapters import claude_ledger, codex_ledger

T0 = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)


def _stamp(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _write(path: Path, entries: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    return path


def _filler(count: int, start: float, *, width: int = 600) -> list[dict[str, Any]]:
    """Entries of the work done after: one a second from ``start``, ``width`` bytes each."""
    return [
        {"type": "assistant", "timestamp": _stamp(start + n), "pad": "x" * width}
        for n in range(count)
    ]


def _is_marker(entry: dict[str, Any]) -> bool:
    return entry.get("type") == "marker"


def _marker(seconds: float) -> dict[str, Any]:
    return {"type": "marker", "timestamp": _stamp(seconds)}


# --- recorded_since: far back is found, a near answer is cheap -----------------------------


def test_an_entry_far_back_is_found_by_widening_the_look(tmp_path: Path) -> None:
    """The entry is 30 KB from the end and the first look is 1 KB: it takes widening, three
    times (4 KB, 16 KB, 64 KB), to reach it."""
    path = _write(tmp_path / "t.jsonl", [_marker(0), *_filler(60, 1)])
    assert path.stat().st_size > 30_000

    assert ledger.recorded_since(path, T0 - timedelta(seconds=5), _is_marker, tail_bytes=1_000)
    assert ledger.recorded_since(path, T0, _is_marker, tail_bytes=1_000)


def test_no_such_entry_is_no_whatever_the_widening(tmp_path: Path) -> None:
    path = _write(tmp_path / "t.jsonl", _filler(60, 1))

    assert not ledger.recorded_since(path, T0 - timedelta(seconds=5), _is_marker, tail_bytes=1_000)


def test_an_entry_older_than_since_is_not_one_since_then(tmp_path: Path) -> None:
    path = _write(tmp_path / "t.jsonl", [_marker(0), *_filler(60, 1)])

    assert not ledger.recorded_since(path, T0 + timedelta(seconds=1), _is_marker, tail_bytes=1_000)


def test_a_look_that_reaches_since_goes_no_further_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end of the transcript already holds an entry older than ``since``: the answer is
    in what was read, and the next, wider look would read four times as much for nothing."""
    path = _write(tmp_path / "t.jsonl", [_marker(0), *_filler(600, 1)])
    looked: list[int] = []
    real = transcripts.tail_entries

    def spy(file: Path, window: int, since: int = 0) -> Any:
        looked.append(window)
        return real(file, window, since)

    monkeypatch.setattr(transcripts, "tail_entries", spy)

    # since lies inside the last 4 KB: the first look finds an older entry in it.
    assert not ledger.recorded_since(
        path, T0 + timedelta(seconds=599), _is_marker, tail_bytes=4_000
    )

    assert looked == [4_000], looked


def test_each_look_is_four_times_the_one_before_up_to_the_whole_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path / "t.jsonl", [_marker(0), *_filler(60, 1)])
    size = path.stat().st_size
    looked: list[int] = []
    real = transcripts.tail_entries

    def spy(file: Path, window: int, since: int = 0) -> Any:
        looked.append(window)
        return real(file, window, since)

    monkeypatch.setattr(transcripts, "tail_entries", spy)

    assert (
        ledger.recorded_since(path, T0 - timedelta(seconds=5), lambda e: False, tail_bytes=3_000)
        is False
    )

    assert looked[0] == 3_000
    assert all(b == a * 4 for a, b in itertools.pairwise(looked)), looked
    assert looked[-1] >= size > looked[-2], "the last look takes the whole file, and is the last"


# --- Claude Code's compacted_since: a compaction followed by 700 KB of work -----------------

_BOUNDARY = {"type": "system", "subtype": "compact_boundary"}


def _claude(path: Path, compacted_at: float, after: int) -> Path:
    return _write(
        path,
        [
            *_filler(3, compacted_at - 10, width=50),
            {**_BOUNDARY, "timestamp": _stamp(compacted_at)},
            *_filler(after, compacted_at + 1, width=1_000),
        ],
    )


def test_claude_codes_compaction_700_kb_back_is_found(tmp_path: Path) -> None:
    path = _claude(tmp_path / "t.jsonl", 100, 700)
    assert path.stat().st_size > 700_000

    assert claude_ledger.compacted_since(path, T0 + timedelta(seconds=99))
    assert claude_ledger.compacted_since(path, T0 + timedelta(seconds=100))


def test_claude_codes_compaction_is_one_since_a_moment_only_if_it_is_not_older(
    tmp_path: Path,
) -> None:
    path = _claude(tmp_path / "t.jsonl", 100, 700)

    assert not claude_ledger.compacted_since(path, T0 + timedelta(seconds=101))
    assert not claude_ledger.compacted_since(
        _write(tmp_path / "none.jsonl", _filler(700, 1, width=1_000)), T0 - timedelta(seconds=1)
    )


def test_only_a_compact_boundary_is_a_compaction_of_claude_codes(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "t.jsonl",
        [
            {"type": "system", "subtype": "other", "timestamp": _stamp(1)},
            {"type": "assistant", "subtype": "compact_boundary", "timestamp": _stamp(2)},
        ],
    )

    assert not claude_ledger.compacted_since(path, T0)


# --- Codex's, the same walk -----------------------------------------------------------------


def test_codexs_compaction_400_kb_back_is_found(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "rollout.jsonl",
        [
            {"type": "compacted", "timestamp": _stamp(50), "payload": {}},
            *_filler(400, 51, width=1_000),
        ],
    )

    assert codex_ledger.compacted_since(path, T0 + timedelta(seconds=40))
    assert not codex_ledger.compacted_since(path, T0 + timedelta(seconds=51))
