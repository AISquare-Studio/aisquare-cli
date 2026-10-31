"""#23: the multi-instance pieces, combined. Where two pieces meet, each asks the other.

#463 (pane size) decided whether the asq that sizes a shared window was over SSH from that
asq's own environment, and whether it still ran from its pid alone; #462 (the instance
registry) knows both better: a kind its beat keeps fresh (a tmux reattached over SSH), and
which pids are live asqs (manager's addendum 5300, from review 5299).
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from aisquare.cli.ui import terminal
from aisquare.cli.ui.terminal import TerminalPane, size_owner_runs, sized_elsewhere
from aisquare.core import instances
from aisquare.core.tmux import SizeOwner
from tests.asq_elsewhere import asq_running


@pytest.fixture
def registered(monkeypatch: pytest.MonkeyPatch) -> Iterator[instances.Registration]:
    """This process registered as an asq, its beat thread reading the others every ~20 ms."""
    monkeypatch.setattr(instances, "attached_kind", lambda env=None: "native")
    monkeypatch.setattr(instances, "HEARTBEAT_SECONDS", 0.02)
    monkeypatch.setattr(instances, "POLL_SECONDS", 0.01)  # the thread wakes this often
    with instances.gate():
        registration = instances.register(instances.describe_this_process(kind="native"))
    registration.start()
    yield registration
    registration.deregister()


def _beaten(registration: instances.Registration, pids: set[int]) -> None:
    """Wait for a beat whose read of the others names exactly ``pids``."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if {other.pid for other in registration.others} == pids:
            time.sleep(0.05)  # and one beat more, so the read is newer than any claim before
            return
        time.sleep(0.01)
    pytest.fail(f"the beat read {registration.others}")


# --- the claim's kind -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "environ", "where"),
    [
        ("ssh", {}, "ssh"),  # a tmux reattached over SSH: the env says nothing of it
        ("native", {"SSH_CONNECTION": "10.0.0.2 51234 10.0.0.1 22"}, "mac"),  # stale env
    ],
    ids=["reattached-over-ssh", "stale-ssh-env"],
)
def test_a_claim_says_the_terminal_the_registry_knows_not_the_environment(
    kind: str, environ: dict[str, str], where: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("SSH_CONNECTION", "SSH_TTY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "platform", "darwin")
    with instances.gate():
        registration = instances.register(instances.describe_this_process(kind=kind))  # type: ignore[arg-type]
    try:
        assert terminal._claim(os.getpid()).where == where
    finally:
        registration.deregister()


def test_with_no_registration_a_claim_reads_the_environment_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.2 51234 10.0.0.1 22")
    assert instances.this_registration() is None
    assert terminal._claim(os.getpid()).where == "ssh"


def test_the_hint_names_the_owners_terminal_as_its_record_says(
    registered: instances.Registration, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An owner that claimed as ``mac`` and has since been reattached over SSH: its record,
    kept fresh by its beat, says ssh, and so does the hint."""
    monkeypatch.setattr(sys, "platform", "darwin")
    owner_pid = os.getppid()
    with asq_running(pid=owner_pid, kind="ssh"):
        _beaten(registered, {owner_pid})
        claimed = SizeOwner(owner_pid, "mini", "mac", time.time_ns())

        assert sized_elsewhere(claimed) == "[sized by the other asq (ssh)]"

    assert sized_elsewhere(claimed._replace(pid=2**22 + 1)) == "[sized by the other asq (Mac)]"


# --- the owner's liveness ---------------------------------------------------------------------


def test_an_owner_whose_pid_runs_but_is_no_live_asq_has_let_go(
    registered: instances.Registration,
) -> None:
    """A claim whose asq ended and whose pid another process has since: the pid runs, so the
    window stayed "sized by the other asq" for good. The registry says no asq has that pid."""
    stranger = os.getppid()  # alive, and no asq
    stale_claim = SizeOwner(stranger, "mini", "ssh", time.time_ns())
    _beaten(registered, set())
    pane = SimpleNamespace(owner_pid=os.getpid())

    assert not size_owner_runs(stale_claim)
    assert TerminalPane._other_owner(pane, stale_claim.value()) is None  # type: ignore[arg-type]
    assert TerminalPane._ended_claim(pane, stale_claim.value()) == stale_claim.value()  # type: ignore[arg-type]

    with asq_running(pid=stranger):  # control: a live asq with that pid
        _beaten(registered, {stranger})
        assert size_owner_runs(stale_claim)
        assert TerminalPane._other_owner(pane, stale_claim.value()) == stale_claim  # type: ignore[arg-type]


def test_a_claim_newer_than_the_last_read_of_the_registry_counts(
    registered: instances.Registration, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An asq that started and claimed since the beat last read: the read could not know it."""
    _beaten(registered, set())
    monkeypatch.setattr(instances, "HEARTBEAT_SECONDS", 60.0)  # no read after this one
    time.sleep(0.05)
    fresh = SizeOwner(os.getppid(), "mini", "ssh", time.time_ns())

    assert fresh.stamp > registered.others_read_ns and size_owner_runs(fresh)


def test_with_no_registration_the_pid_alone_decides_as_before() -> None:
    assert instances.this_registration() is None
    assert size_owner_runs(SizeOwner(os.getppid(), "mini", "ssh", 1))
    assert not size_owner_runs(SizeOwner(2**22 + 1, "mini", "ssh", 1))
