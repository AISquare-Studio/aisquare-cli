"""Entry id format, uniqueness, and time-ordering."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterator

import pytest

from aisquare.core import ids
from aisquare.core.ids import ENTRY_PREFIX, TASK_PREFIX, new_entry_id, new_task_id

_ID_PATTERN = re.compile(r"^ctx_[0-9abcdefghjkmnpqrstvwxyz]{26}$")
_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"


_A_BURST_BEHIND_THE_WORKER = 4_000_000_000_000


@pytest.fixture(scope="module", autouse=True)
def _the_worker_is_found_with_a_burst_behind_it_and_left_with_it() -> Iterator[None]:
    """What an earlier test file left in this worker: ids stamped far ahead of the clock. Each
    test below must start from none (``test_a_test_starts...``) and hand this back, which is
    asserted once the module's last test is done: dropping either half of the fixture under it
    fails one of the two, where each half alone used to pass."""
    found = ids._LAST_MS[0]
    ids._LAST_MS[0] = _A_BURST_BEHIND_THE_WORKER
    yield
    handed_back = ids._LAST_MS[0]
    ids._LAST_MS[0] = found
    assert handed_back == _A_BURST_BEHIND_THE_WORKER, "a test left the worker as it was not found"


@pytest.fixture(autouse=True)
def _each_test_starts_from_a_clock_that_no_burst_ran_ahead_of() -> Iterator[None]:
    """The millisecond of the last id is process state. A test that freezes ``time.time`` in
    2096 leaves it there, and every id the worker makes after is stamped in 2096, one
    millisecond apart; ``test_the_clock_catches_up...`` read it as its own burst and failed
    when it ran after one. Each test here starts from none and the worker gets its own back."""
    ran_ahead_to = ids._LAST_MS[0]
    ids._LAST_MS[0] = 0
    yield
    ids._LAST_MS[0] = ran_ahead_to


def test_a_test_starts_from_no_burst() -> None:
    assert ids._LAST_MS[0] == 0, "the worker's burst is not this test's"


def _without_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test that mints hundreds of ids in a frozen millisecond, or thousands in a burst, asks
    for the order or the uniqueness of them, not for the clock to be waited for."""
    monkeypatch.setattr(ids, "_MAX_LEAD_MS", 10**9)


class _Clock:
    """A clock that moves only when something sleeps on it, by the whole milliseconds slept: a
    burst of ids against it takes no time, and the lead it runs up is read off exactly. It
    reads half a millisecond in, so that ``int(time.time() * 1000)`` never rounds down one."""

    def __init__(
        self,
        start_ms: int,
        monkeypatch: pytest.MonkeyPatch,
        *,
        moves_by: float = 1.0,
        late_by_ms: int = 0,
    ) -> None:
        """``moves_by`` is how much of a sleep the clock moves (0: it is stopped) and
        ``late_by_ms`` how long past it the sleeper wakes (a thread the scheduler left)."""
        self.ms = start_ms
        self.slept: list[float] = []
        self._moves_by = moves_by
        self._late_by_ms = late_by_ms
        monkeypatch.setattr(time, "time", lambda: (self.ms + 0.5) / 1000)
        monkeypatch.setattr(time, "sleep", self.sleep)

    def sleep(self, seconds: float) -> None:
        assert len(self.slept) < 10_000, "an id is waiting on a clock that does not move"
        self.slept.append(seconds)
        self.ms += round(seconds * 1000 * self._moves_by) + self._late_by_ms


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


def test_ids_are_unique(monkeypatch: pytest.MonkeyPatch) -> None:
    _without_the_cap(monkeypatch)
    made = {new_entry_id() for _ in range(5_000)}
    assert len(made) == 5_000


def test_ids_sort_by_creation_time() -> None:
    # The 48-bit millisecond prefix makes ids lexicographically time-ordered, so
    # a plain ``ORDER BY id`` lists entries oldest-first. A small gap guarantees
    # the two ids land in different milliseconds.
    earlier = new_entry_id()
    time.sleep(0.005)
    later = new_entry_id()
    assert earlier < later


def test_ids_made_in_one_millisecond_sort_in_the_order_they_were_made(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The docstring's promise, without the gap the test above needs: a script's ``task add``
    loop makes many ids in one millisecond, and a Windows clock reads the same one for ~15 ms.
    Listed by ``ORDER BY id`` a board's tasks came back #1, #3, #2."""
    _without_the_cap(monkeypatch)
    made = [new_task_id() for _ in range(5_000)]
    assert made == sorted(made)
    assert len(set(made)) == len(made)


def test_a_burst_the_cap_holds_back_keeps_its_order_on_the_real_clock() -> None:
    """Three hundred ids in a tight loop pass the bound six times over, and each past it waits
    for the clock a millisecond or so: nothing is dropped, repeated or reordered."""
    made = [new_task_id() for _ in range(300)]
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
    _without_the_cap(monkeypatch)
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
    clock = _Clock(4_050_000_000_000, monkeypatch)
    before = new_entry_id()
    clock.ms = 1_000  # an NTP step, a restored VM
    after = new_entry_id()
    assert before < after


def test_threads_never_take_one_millisecond_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """The clock is frozen, and the first time each thread reads it, outside the lock, it waits
    for the other seven to be there too: all of them have then read the same last millisecond
    and none has written, which is where unlocked threads take the same next one. Inside the
    lock the others cannot be at the clock, so nobody waits; no timing decides either way."""
    threads_count = 8
    rendezvous = threading.Barrier(threads_count)
    waited: set[threading.Thread] = set()
    made: list[list[str]] = [[] for _ in range(threads_count)]

    def make(into: list[str]) -> None:
        into.extend(new_task_id() for _ in range(300))

    threads = [threading.Thread(target=make, args=(into,)) for into in made]

    def reads_the_frozen_clock() -> float:
        me = threading.current_thread()
        # Only these eight: any other thread that reads the clock while it is frozen (a worker's
        # own, a daemon an earlier test left) is not a party to the rendezvous.
        if me in threads and me not in waited and not ids._LAST_LOCK.locked():
            waited.add(me)
            rendezvous.wait(timeout=30)
        return 4_200_000_000.0

    monkeypatch.setattr(time, "time", reads_the_frozen_clock)
    _without_the_cap(monkeypatch)
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    everything = [entry_id for into in made for entry_id in into]
    assert len(everything) == threads_count * 300
    assert len({_stamp(entry_id, TASK_PREFIX) for entry_id in everything}) == 2_400
    assert all(into == sorted(into) for into in made), "each thread's own ids are in its order"


# --- the lead of a burst is held to a bound ----------------------------------------------------


def test_a_burst_runs_no_further_ahead_of_the_clock_than_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a bound, 200 ids of one burst run 199 ms ahead of the clock, and an id another
    process makes at the clock's time sorts before 163 of them (r25 measured it). With it, an id
    that would run past the bound waits for the clock, and the most that can sort after an id
    made at the clock's time is the bound."""
    clock = _Clock(4_300_000_000_000, monkeypatch)
    stamps: list[int] = []
    leads: list[int] = []
    for _ in range(500):
        stamps.append(_stamp(new_task_id(), TASK_PREFIX))
        leads.append(stamps[-1] - clock.ms)
    assert stamps == sorted(set(stamps)), "one stamp each, in order"
    assert max(leads) == ids._MAX_LEAD_MS, "it runs up to the bound and no further"
    assert clock.ms - 4_300_000_000_000 >= 500 - ids._MAX_LEAD_MS - 1, "and waited for the rest"
    after_the_burst = stamps[-1] + 1 - ids._MAX_LEAD_MS
    assert sum(1 for stamp in stamps if stamp >= after_the_burst) <= ids._MAX_LEAD_MS + 1


def test_an_id_another_process_makes_meanwhile_sorts_before_at_most_the_bound_of_a_burst(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(4_310_000_000_000, monkeypatch)
    burst = [_stamp(new_task_id(), TASK_PREFIX) for _ in range(200)]
    others_stamp = clock.ms  # a second process, whose clock reads what the first one's does
    assert sum(1 for stamp in burst if stamp > others_stamp) <= ids._MAX_LEAD_MS


def test_the_wait_is_for_the_excess_only(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock(4_320_000_000_000, monkeypatch)
    for _ in range(ids._MAX_LEAD_MS + 1):
        new_entry_id()
    assert clock.slept == [], "within the bound nothing waits"
    new_entry_id()
    assert clock.slept == [0.001], "one millisecond over: one millisecond"


def test_a_clock_that_stepped_back_a_long_way_is_not_waited_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lead of a year is no burst's: waiting for it would hold every id for as long as the
    step is long. The ids go on, one millisecond apart, as they did."""
    clock = _Clock(4_330_000_000_000, monkeypatch)
    before = new_entry_id()
    clock.ms -= 31_536_000_000  # a restored VM, a clock set wrong and then right
    after = [new_entry_id() for _ in range(5)]
    assert clock.slept == []
    assert [before, *after] == sorted([before, *after])


def test_a_clock_that_does_not_move_delays_an_id_by_one_wait_and_no_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tests freeze the clock, and a clock under a debugger or a suspended VM stops: an id waits
    once and takes the next millisecond, not for a time that does not come."""
    clock = _Clock(4_340_000_000_000, monkeypatch, moves_by=0)
    made = [new_entry_id() for _ in range(2 * ids._MAX_LEAD_MS)]
    assert made == sorted(made) and len(set(made)) == len(made)
    assert len(clock.slept) <= len(made), "at most one wait an id"


def test_an_id_that_slept_past_the_clock_takes_the_time_it_woke_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The thread the scheduler left for a second comes back to a clock a second on: its id is
    stamped by that clock, not by the millisecond after its own burst, so that what another
    process made in the second it lost sorts before it."""
    clock = _Clock(4_350_000_000_000, monkeypatch, late_by_ms=1_000)
    for _ in range(ids._MAX_LEAD_MS + 1):
        new_entry_id()
    assert _stamp(new_entry_id()) == clock.ms
