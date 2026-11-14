"""Entry id format, uniqueness, and time-ordering."""

from __future__ import annotations

import itertools
import os
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
    """A clock the test moves by hand (``clock.ms``), and nothing sleeps on: ids never wait for
    the clock, so a ``time.sleep`` from ``_new_id`` raises here instead of delaying the run. It
    reads half a millisecond in, so that ``int(time.time() * 1000)`` never rounds down one."""

    def __init__(self, start_ms: int, monkeypatch: pytest.MonkeyPatch) -> None:
        self.ms = start_ms
        monkeypatch.setattr(time, "time", lambda: (self.ms + 0.5) / 1000)
        monkeypatch.setattr(time, "sleep", _no_sleeping)


def _no_sleeping(seconds: float) -> None:
    raise AssertionError(f"an id slept {seconds} s: ids never wait for the clock")


def _stamp(entry_id: str, prefix: str = ENTRY_PREFIX) -> int:
    """The millisecond an id carries."""
    value = 0
    for char in entry_id[len(prefix) : len(prefix) + 10]:
        value = value * 32 + _ALPHABET.index(char)
    return value


def _tail(entry_id: str, prefix: str = ENTRY_PREFIX) -> int:
    """The 80 random bits an id carries."""
    value = 0
    for char in entry_id[len(prefix) + 10 :]:
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


def test_a_burst_past_the_bound_keeps_its_order_on_the_real_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three hundred ids in a tight loop pass the bound six times over, and the ones past it are
    ordered by their tails: nothing is dropped, repeated or reordered, and nothing sleeps."""
    monkeypatch.setattr(time, "sleep", _no_sleeping)
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


@pytest.mark.parametrize("past_the_bound", [False, True], ids=["bumping", "stepping-the-tail"])
def test_threads_never_take_one_millisecond_twice(
    monkeypatch: pytest.MonkeyPatch, past_the_bound: bool
) -> None:
    """The clock is frozen, and the first time each thread reads it, outside the lock, it waits
    for the other seven to be there too: all of them have then read the same last millisecond
    (and tail) and none has written, which is where unlocked threads take the same next one.
    Inside the lock the others cannot be at the clock, so nobody waits; no timing decides either
    way. Past the bound the ids share a millisecond, and the tails are what must not repeat or
    run backward in a thread."""
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
    if past_the_bound:
        ids._LAST_MS[0] = 4_200_000_000_000 + 10 * ids._MAX_LEAD_MS
    else:
        _without_the_cap(monkeypatch)
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    everything = [entry_id for into in made for entry_id in into]
    assert len(everything) == threads_count * 300
    if past_the_bound:
        assert len(set(everything)) == 2_400
        assert len({_stamp(entry_id, TASK_PREFIX) for entry_id in everything}) == 1
    else:
        assert len({_stamp(entry_id, TASK_PREFIX) for entry_id in everything}) == 2_400
    assert all(into == sorted(into) for into in made), "each thread's own ids are in its order"


# --- the lead of a burst is held to a bound, and nothing waits for the clock -------------------

_BASE_MS = 4_300_000_000_000


def _a_burst(
    clock: _Clock, count: int, *, clock_moves_every: int | None
) -> tuple[list[str], list[int]]:
    """``count`` ids made in a loop that does nothing else, against ``clock`` (stopped, or one
    millisecond on after every ``clock_moves_every`` ids): the ids, and how far ahead of the
    clock each one was when it was made."""
    made: list[str] = []
    leads: list[int] = []
    for n in range(count):
        made.append(new_task_id())
        leads.append(_stamp(made[-1], TASK_PREFIX) - clock.ms)
        if clock_moves_every is not None and n % clock_moves_every == clock_moves_every - 1:
            clock.ms += 1
    return made, leads


@pytest.mark.parametrize("clock_moves_every", [None, 100, 3], ids=["stopped", "slow", "quick"])
def test_a_burst_of_two_thousand_ids_is_in_order_and_runs_no_further_ahead_than_the_bound(
    monkeypatch: pytest.MonkeyPatch, clock_moves_every: int | None
) -> None:
    """Without a bound, 200 ids of one burst run 199 ms ahead of the clock, and an id another
    process makes at the clock's time sorts before 163 of them (r25 measured it). With it the lead
    is the bound at most, so what a second process's id sorts before is 50 ms of the burst's
    time and no more, and nothing waits for the clock to come up to it: ``_Clock`` raises at a
    ``time.sleep``."""
    clock = _Clock(_BASE_MS, monkeypatch)
    made, leads = _a_burst(clock, 2_000, clock_moves_every=clock_moves_every)
    assert made == sorted(made) and len(set(made)) == len(made), "strictly increasing"
    assert max(leads) == ids._MAX_LEAD_MS, "it runs up to the bound and no further"
    assert min(leads) >= 0


def test_the_ids_within_the_bound_still_differ_inside_the_first_fourteen_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Up to the bound each id has a millisecond of its own, so that a short prefix of one
    (``find_task``'s, ``task show``'s) resolves as it did; past it they share the millisecond and
    are told apart by the tail."""
    clock = _Clock(_BASE_MS, monkeypatch)
    made, _ = _a_burst(clock, 2_000, clock_moves_every=None)
    within = made[: ids._MAX_LEAD_MS + 1]
    assert len({entry_id[: len(TASK_PREFIX) + 10] for entry_id in within}) == len(within)
    past = made[ids._MAX_LEAD_MS :]
    assert {_stamp(entry_id, TASK_PREFIX) for entry_id in past} == {_BASE_MS + ids._MAX_LEAD_MS}
    assert len({_tail(entry_id, TASK_PREFIX) for entry_id in past}) == len(past)


def test_past_the_bound_an_id_stays_in_the_millisecond_with_the_tail_a_step_above_the_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The step is drawn from the bytes ``os.urandom`` gives: a fixed draw makes it exact."""
    clock = _Clock(_BASE_MS, monkeypatch)
    for _ in range(ids._MAX_LEAD_MS + 1):
        new_entry_id()
    at_the_bound = new_entry_id()
    assert _stamp(at_the_bound) == _BASE_MS + ids._MAX_LEAD_MS
    monkeypatch.setattr(os, "urandom", lambda n: bytes(n - 1) + b"\x07")  # a step of 7 + 1
    after = [new_entry_id() for _ in range(3)]
    assert [_stamp(entry_id) for entry_id in after] == [_BASE_MS + ids._MAX_LEAD_MS] * 3
    assert [_tail(entry_id) for entry_id in after] == [
        _tail(at_the_bound) + 8 * n for n in (1, 2, 3)
    ]
    assert clock.ms == _BASE_MS


def test_past_the_bound_the_step_is_at_random_and_up_to_two_to_the_fortieth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A step of one would make the next id of a burst a guess; one of a few bits would make
    it a short search. Three hundred steps, each in 1 to 2**40, no two alike, some of them big."""
    _Clock(_BASE_MS, monkeypatch)
    for _ in range(ids._MAX_LEAD_MS + 1):
        new_entry_id()
    tails = [_tail(new_entry_id()) for _ in range(301)]
    steps = [later - earlier for earlier, later in itertools.pairwise(tails)]
    assert min(steps) >= 1 and max(steps) <= 2**40
    assert max(steps) > 2**39, "drawn from the whole range, not a corner of it"
    assert len(set(steps)) == len(steps), "not the same step again"


def test_a_tail_that_would_overflow_takes_the_next_millisecond(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(_BASE_MS, monkeypatch)
    for _ in range(ids._MAX_LEAD_MS + 1):
        new_entry_id()
    at_the_bound = _BASE_MS + ids._MAX_LEAD_MS
    monkeypatch.setattr(os, "urandom", lambda n: bytes(n))  # a step of 1; a fresh tail of 0
    ids._LAST_TAIL[0] = 2**80 - 2
    last_that_fits = new_entry_id()
    assert (_stamp(last_that_fits), _tail(last_that_fits)) == (at_the_bound, 2**80 - 1)
    rolled = new_entry_id()
    assert _stamp(rolled) == at_the_bound + 1, "one more would not fit in 80 bits"
    assert rolled > last_that_fits
    # And it goes on from there: the bound counts from the clock, which has not moved.
    again = new_entry_id()
    assert (_stamp(again), _tail(again)) == (at_the_bound + 1, 1) and again > rolled
    assert clock.ms == _BASE_MS


def test_a_clock_that_does_not_move_delays_no_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests freeze the clock, and a clock under a debugger or a suspended VM stops: no id waits
    for a time that does not come, and each is above the one before."""
    _Clock(_BASE_MS + 10, monkeypatch)
    made = [new_entry_id() for _ in range(2_000)]
    assert made == sorted(made) and len(set(made)) == len(made)


@pytest.mark.parametrize(
    "step_back_ms", [ids._MAX_LEAD_MS + 10, 5_000, 31_536_000_000], ids=["60ms", "5s", "a-year"]
)
def test_a_clock_that_stepped_back_is_not_waited_for(
    monkeypatch: pytest.MonkeyPatch, step_back_ms: int
) -> None:
    """A lead of a year is no burst's, and waiting for it would hold every id for as long as the
    step is long; neither is one of a minute. The ids go on, above the ones before."""
    clock = _Clock(_BASE_MS + 40_000_000_000, monkeypatch)
    before = [new_entry_id() for _ in range(5)]
    clock.ms -= step_back_ms
    after = [new_entry_id() for _ in range(2_000)]
    assert [*before, *after] == sorted([*before, *after])
    assert len(set(after)) == len(after)
    assert {_stamp(entry_id) for entry_id in after} == {_stamp(before[-1])}, "and no further ahead"


def test_the_clock_catches_up_with_a_burst_past_the_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ten milliseconds on there is still a lead to hold, and the ids take the milliseconds
    after the last again; once the clock is past the burst's, the clock's own."""
    clock = _Clock(_BASE_MS, monkeypatch)
    _a_burst(clock, 500, clock_moves_every=None)
    at_the_bound = _BASE_MS + ids._MAX_LEAD_MS
    clock.ms += 10
    assert _stamp(new_entry_id()) == at_the_bound + 1, "the lead is 41 ms: within the bound"
    clock.ms = at_the_bound + 5
    assert _stamp(new_entry_id()) == clock.ms, "the clock is past it"
