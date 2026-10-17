"""``core.locking.holding``: the one bounded wait every lock file in the home goes through.

``state.json``'s, the credentials file's and a brain's each had their own copy of the
open-then-poll loop, and the fleet's branch lock a fourth (review of #259, round 17, 11/12).
The copies had drifted: a brain read every ``OSError`` as "held" and waited out its whole
``wait_s`` for an error no retry clears. Remote's ``remote.json`` lock and its claim of the
home were two more (round 18, 8/12).
"""

from __future__ import annotations

import errno
import os
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from aisquare.core import brain, credentials, locking, state_file
from aisquare.services import remote_server


def _state(home: Path) -> None:
    state_file.update_state("board_theme", "nord")


def _credentials(home: Path) -> None:
    with credentials._locked(home / "credentials.json"):
        pass


def _brain(home: Path) -> None:
    with brain._lock(home / "brain", wait_s=5.0) as won:
        assert won


def _remote_json(home: Path) -> None:
    fd = remote_server._lock_state_file(home / "remote.json")
    assert fd is not None, "taken"
    locking.unlock(fd)
    os.close(fd)


def _remote_claim(home: Path) -> None:
    """Remote's claim of its home, whose patience (50 ms) two "held" answers fit in."""
    state = cast(Any, SimpleNamespace(_state_path=home / "remote.json"))
    try:
        assert remote_server._claim_remote_home(state, by="serve", port=8750) is True
    finally:
        remote_server._release_remote_home()


TAKERS: dict[str, Callable[[Path], None]] = {
    "state.json": _state,
    "credentials": _credentials,
    "brain": _brain,
    "remote.json": _remote_json,
    "remote claim": _remote_claim,
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


def test_whether_another_process_serves_remote_is_asked_once_through_the_one_loop(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Remote's look at whether another process serves its home asks the lock once: "held"
    is another Remote; any other answer is no evidence of one."""
    isolated_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(remote_server, "_home_claim", None)
    (isolated_home / "remote-serve.lock").touch()
    answers = [OSError(errno.EACCES, "held"), OSError(errno.ENOLCK, "No locks available")]
    asked: list[int] = []

    def answer(fd: int) -> None:
        asked.append(fd)
        raise answers.pop(0)

    monkeypatch.setattr(locking, "lock_exclusive", answer)
    assert remote_server.remote_served_elsewhere() is True
    assert remote_server.remote_served_elsewhere() is False
    assert len(asked) == 2, "once each"


def test_a_lock_file_this_user_may_not_write_still_says_another_process_serves(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 19: the claim of the home opens its lock file read-only when it
    may not be written (``0400``, or left by a ``sudo aisquare remote serve``), and the look
    at whether another process serves opened it read-write only, and said nobody did. Both
    open it by the one rule, and the look still makes no file."""
    isolated_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(remote_server, "_home_claim", None)

    def held(fd: int) -> None:
        raise OSError(errno.EACCES, "held")

    monkeypatch.setattr(locking, "lock_exclusive", held)
    lock = isolated_home / "remote-serve.lock"
    assert remote_server.remote_served_elsewhere() is False
    assert not lock.exists(), "the look made a lock file"
    lock.touch()
    lock.chmod(0o400)
    try:
        try:
            os.close(os.open(lock, os.O_RDWR))
        except PermissionError:
            pass
        else:
            pytest.skip("this user writes a 0400 file (root)")
        assert remote_server.remote_served_elsewhere() is True
    finally:
        lock.chmod(0o600)
