"""The fleet's memory probe (#283): ``/proc`` on Linux and WSL, ``sysctl`` and ``vm_stat`` on macOS.

Every case reads fixture text, never this machine: the suite's ``roomy_memory`` fixture
stands in for ``headroom()`` everywhere else, and these are the readers behind it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from aisquare.core import memory

#: The real dispatcher, taken at import, before the suite's ``roomy_memory`` fixture
#: replaces ``memory.headroom`` for every test.
REAL_HEADROOM = memory.headroom

MEMINFO = """MemTotal:       16303428 kB
MemFree:          812344 kB
MemAvailable:    1536000 kB
Buffers:          123456 kB
"""

#: vm_stat on macOS 26 (Apple silicon), as measured here: a 16 KiB page.
VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                   341738.
Pages active:                                1224203.
Pages inactive:                              1217864.
Pages speculative:                              5055.
Pages throttled:                                   0.
Pages wired down:                             401520.
"""


def _proc(tmp_path: Path, psi: str | None) -> memory.Headroom:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(MEMINFO, encoding="ascii")
    pressure = tmp_path / "pressure"
    if psi is not None:
        pressure.write_text(psi, encoding="ascii")
    return memory.linux_headroom(meminfo, pressure)


def test_linux_reads_memavailable_not_memfree(tmp_path: Path) -> None:
    head = _proc(tmp_path, None)
    assert (head.available_mb, head.total_mb) == (1500, 15921)
    assert head.pressure == "normal" and head.psi_some_avg10 is None
    assert head.source == "proc"


@pytest.mark.parametrize(
    ("avg10", "pressure"),
    [
        ("0.00", "normal"),
        ("9.99", "normal"),
        ("10.00", "warn"),
        ("39.9", "warn"),
        ("55.00", "critical"),
    ],
)
def test_linux_psi_some_avg10_sets_the_pressure(tmp_path: Path, avg10: str, pressure: str) -> None:
    psi = (
        f"some avg10={avg10} avg60=1.00 avg300=0.50 total=12345\n"
        "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
    )
    head = _proc(tmp_path, psi)
    assert head.pressure == pressure
    assert head.psi_some_avg10 == float(avg10)


def test_a_kernel_without_psi_is_judged_by_memory_alone(tmp_path: Path) -> None:
    head = _proc(tmp_path, None)
    assert head.pressure == "normal" and head.known


def test_darwin_reads_the_pressure_level_memsize_and_vm_stat() -> None:
    answers = {"kern.memorystatus_vm_pressure_level": "1", "hw.memsize": "51539607552"}
    head = memory.darwin_headroom(answers.get, lambda: VM_STAT)
    # (341738 + 1217864 + 5055) pages x 16384 bytes
    assert head.available_mb == (341738 + 1217864 + 5055) * 16384 // (1024 * 1024)
    assert head.total_mb == 49152
    assert (head.pressure, head.source) == ("normal", "darwin")


@pytest.mark.parametrize(
    ("level", "pressure"),
    [("1", "normal"), ("2", "warn"), ("4", "critical"), ("", "unknown"), ("7", "unknown")],
)
def test_darwin_pressure_levels(level: str, pressure: str) -> None:
    head = memory.darwin_headroom(
        {"kern.memorystatus_vm_pressure_level": level}.get, lambda: VM_STAT
    )
    assert head.pressure == pressure


def test_darwin_output_that_is_not_vm_stat_is_no_number() -> None:
    assert memory.darwin_available_bytes("") is None
    assert (
        memory.darwin_available_bytes(
            "Mach Virtual Memory Statistics: (page size of 4096 bytes)\nPages free: 10.\n"
        )
        is None
    )
    head = memory.darwin_headroom(lambda _name: None, lambda: None)
    assert head == memory.Headroom(None, None, "unknown", None, "darwin")
    assert not head.known


def test_headroom_never_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A probe is an answer or unknown, never a crash at spawn."""
    present = tmp_path / "meminfo"
    present.write_text(MEMINFO, encoding="ascii")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(memory, "MEMINFO", present)

    def broken(meminfo: Path, psi: Path) -> memory.Headroom:
        raise PermissionError("no /proc")

    monkeypatch.setattr(memory, "linux_headroom", broken)
    assert REAL_HEADROOM() == memory.UNKNOWN


def test_linux_dispatch_reads_proc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    present = tmp_path / "meminfo"
    present.write_text(MEMINFO, encoding="ascii")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(memory, "MEMINFO", present)
    monkeypatch.setattr(memory, "PSI_MEMORY", tmp_path / "no-psi")
    assert REAL_HEADROOM().available_mb == 1500


def test_elsewhere_is_unknown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(memory, "MEMINFO", tmp_path / "absent")
    assert REAL_HEADROOM() == memory.UNKNOWN
