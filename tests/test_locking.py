"""``core.locking.holding``: the one bounded wait every lock file in the home goes through.

``state.json``'s, the credentials file's and a brain's each had their own copy of the
open-then-poll loop, and the fleet's branch lock a fourth (review of #259, round 17, 11/12).
The copies had drifted: a brain read every ``OSError`` as "held" and waited out its whole
``wait_s`` for an error no retry clears.
"""

from __future__ import annotations

import errno
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from aisquare.core import brain, credentials, locking, state_file


def _state(home: Path) -> None:
    state_file.update_state("board_theme", "nord")


def _credentials(home: Path) -> None:
    with credentials._locked(home / "credentials.json"):
        pass


def _brain(home: Path) -> None:
    with brain._lock(home / "brain", wait_s=5.0) as won:
        assert won


TAKERS: dict[str, Callable[[Path], None]] = {
    "state.json": _state,
    "credentials": _credentials,
    "brain": _brain,
}


@pytest.mark.parametrize("taker", list(TAKERS))
def test_every_lock_file_waits_through_held_in_the_one_loop(
    taker: str, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each lock asks ``core.locking``'s primitive, through its one loop: two "held" answers
    waited through (``EACCES`` is Windows' ``LK_NBLCK``), then the lock taken."""
    isolated_home.mkdir(parents=True, exist_ok=True)
    real = locking.lock_exclusive
    attempts: list[int] = []

    def held_twice(fd: int) -> None:
        attempts.append(fd)
        if len(attempts) <= 2:
            raise OSError(errno.EACCES, "held")
        real(fd)

    monkeypatch.setattr(locking, "lock_exclusive", held_twice)

    TAKERS[taker](isolated_home)

    assert len(attempts) == 3


def test_a_brain_lock_the_platform_refuses_is_not_won_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A brain read every ``OSError`` as "held" and polled out its ``wait_s`` (five seconds for
    a recall) for an ``ENOLCK`` no retry clears. Not won, still — at once."""

    def no_locks(fd: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(locking, "lock_exclusive", no_locks)
    started = time.monotonic()
    with brain._lock(tmp_path / "brain", wait_s=5.0) as won:
        assert not won
    assert time.monotonic() - started < 1.0


def test_holding_raises_timeout_past_its_wait_and_the_block_runs_once_free(
    tmp_path: Path,
) -> None:
    path = tmp_path / "x.lock"
    with (
        locking.holding(path, wait_s=0),
        pytest.raises(TimeoutError),
        locking.holding(path, wait_s=0.05),
    ):
        pytest.fail("a second holder got in")
    with locking.holding(path, wait_s=0):
        pass
