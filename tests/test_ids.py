"""Entry id format, uniqueness, and time-ordering."""

from __future__ import annotations

import re
import threading
import time

import pytest

from aisquare.core.ids import ENTRY_PREFIX, TASK_PREFIX, new_entry_id, new_task_id

_ID_PATTERN = re.compile(r"^ctx_[0-9abcdefghjkmnpqrstvwxyz]{26}$")
_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"


def _stamp(entry_id: str, prefix: str = ENTRY_PREFIX) -> int:
    """The millisecond an id carries."""
    value = 0
    for char in entry_id[len(prefix) : len(prefix) + 10]:
        value = value * 32 + _ALPHABET.index(char)
    return value


def test_id_has_expected_shape() -> None:
    entry_id = new_entry_id()
    assert entry_id.startswith(ENTRY_PREFIX)
    assert _ID_PATTERN.match(entry_id), entry_id


def test_ids_are_unique() -> None:
    ids = {new_entry_id() for _ in range(5_000)}
    assert len(ids) == 5_000


def test_ids_sort_by_creation_time() -> None:
    # The 48-bit millisecond prefix makes ids lexicographically time-ordered, so
    # a plain ``ORDER BY id`` lists entries oldest-first. A small gap guarantees
    # the two ids land in different milliseconds.
    earlier = new_entry_id()
    time.sleep(0.005)
    later = new_entry_id()
    assert earlier < later


def test_ids_made_in_one_millisecond_sort_in_the_order_they_were_made() -> None:
    """The docstring's promise, without the gap the test above needs: a script's ``task add``
    loop makes many ids in one millisecond, and a Windows clock reads the same one for ~15 ms.
    Listed by ``ORDER BY id`` a board's tasks came back #1, #3, #2."""
    made = [new_task_id() for _ in range(5_000)]
    assert made == sorted(made)
    assert len(set(made)) == len(made)


def test_ids_of_a_frozen_millisecond_take_the_milliseconds_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "time", lambda: 4_000_000_000.123)  # a millisecond no clock has read
    made = [new_entry_id() for _ in range(50)]
    assert made == sorted(made) and len(set(made)) == 50
    stamps = [_stamp(entry_id) for entry_id in made]
    assert stamps == list(range(stamps[0], stamps[0] + 50)), "one millisecond after the other"


def test_ids_of_one_burst_differ_inside_the_first_fourteen_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A short prefix of an id (``find_task``'s, ``task show``'s) resolves as it did: the
    ids of a burst are told apart by the prefix and the timestamp, not only by their tails."""
    monkeypatch.setattr(time, "time", lambda: 4_000_000_000.123)
    made = [new_entry_id() for _ in range(200)]
    assert len({entry_id[: len(ENTRY_PREFIX) + 10] for entry_id in made}) == 200


def test_the_clock_catches_up_with_a_burst_that_ran_ahead_of_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "time", lambda: 4_100_000_000.000)
    burst = [_stamp(new_entry_id()) for _ in range(3)]
    assert burst == [4_100_000_000_000 + n for n in range(3)]
    monkeypatch.setattr(time, "time", lambda: 4_100_000_000.010)
    assert _stamp(new_entry_id()) == 4_100_000_000_010, "ten milliseconds on, the clock's own"


def test_a_clock_that_steps_back_does_not_reorder_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    before = new_entry_id()
    monkeypatch.setattr(time, "time", lambda: 1.0)  # an NTP step, a restored VM
    after = new_entry_id()
    assert before < after


def test_threads_never_take_one_millisecond_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """The clock is frozen and yields to the other threads as it is read, which is where two
    unlocked threads would both see the same last millisecond and take the same next one."""
    real_sleep = time.sleep

    def reads_the_frozen_clock() -> float:
        real_sleep(0)
        return 4_200_000_000.0

    monkeypatch.setattr(time, "time", reads_the_frozen_clock)
    made: list[list[str]] = [[] for _ in range(8)]

    def make(into: list[str]) -> None:
        into.extend(new_task_id() for _ in range(300))

    threads = [threading.Thread(target=make, args=(into,)) for into in made]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    everything = [entry_id for into in made for entry_id in into]
    assert len({_stamp(entry_id, TASK_PREFIX) for entry_id in everything}) == 2_400
    assert all(into == sorted(into) for into in made), "each thread's own ids are in its order"
