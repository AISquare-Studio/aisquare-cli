"""How much memory this machine has to start another agent: the fleet's admission probe (#283).

``fleet spawn`` used to know one limit, a per-project count, and nothing of the box it
ran on: agents and their test runs could push it into swap while every project sat
under its cap. :func:`headroom` is what admission (``services.fleet.spawn``), the
test-run gate (``fleet gate``) and the doctor's ``fleet capacity`` row read.

- **Linux and WSL**: ``/proc/meminfo`` (``MemAvailable``, the kernel's own "estimate of
  how much memory is available for starting new applications, without swapping") and,
  when the kernel has it, ``/proc/pressure/memory``: ``some avg10`` is the share of the
  last 10 s in which at least one task stalled on memory, warn at 10, critical at 40.
  No subprocess. Under WSL2 these are the VM's numbers, capped by ``.wslconfig``.
- **macOS**: ``sysctl -n kern.memorystatus_vm_pressure_level`` (1 normal, 2 warn,
  4 critical; no root), ``hw.memsize``, and ``vm_stat``'s free + inactive + speculative
  pages, which is what psutil calls ``available`` there. That figure ignores the
  compressor, so the kernel's pressure level is the verdict that wins.
- **Anywhere else** pressure is ``unknown`` and the numbers are ``None``: admission
  admits, and says it could not tell.

Never raises: a probe that cannot be read is an ``unknown`` answer, not a refused spawn.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Pressure = Literal["normal", "warn", "critical", "unknown"]

MEMINFO = Path("/proc/meminfo")
PSI_MEMORY = Path("/proc/pressure/memory")

PSI_WARN = 10.0
"""``some avg10`` at which memory pressure reads ``warn``."""
PSI_CRITICAL = 40.0
"""``some avg10`` at which memory pressure reads ``critical``: admission refuses."""

_DARWIN_LEVELS: dict[str, Pressure] = {"1": "normal", "2": "warn", "4": "critical"}
#: Where macOS keeps ``sysctl``, for a PATH without ``/usr/sbin`` (launchd, cron).
_SYSCTL = ("/usr/sbin/sysctl", "/sbin/sysctl")
_VM_STAT = "/usr/bin/vm_stat"


@dataclass(frozen=True)
class Headroom:
    """Memory available to start something, the machine's total, and how hard it is pressed."""

    available_mb: int | None
    total_mb: int | None
    pressure: Pressure
    psi_some_avg10: float | None
    source: str
    """``proc`` (Linux, WSL), ``darwin``, or ``unknown``: where the numbers came from."""

    @property
    def known(self) -> bool:
        return self.available_mb is not None or self.pressure != "unknown"


UNKNOWN = Headroom(None, None, "unknown", None, "unknown")


def headroom() -> Headroom:
    """This machine's memory headroom now. Never raises; ``unknown`` when it cannot tell."""
    try:
        if sys.platform == "darwin":
            return darwin_headroom(_sysctl, _vm_stat)
        if MEMINFO.is_file():
            return linux_headroom(MEMINFO, PSI_MEMORY)
    except Exception:  # a probe is an answer or unknown, never a crash at spawn
        return UNKNOWN
    return UNKNOWN


def linux_headroom(meminfo: Path, psi: Path) -> Headroom:
    """``/proc/meminfo`` and, when present, ``/proc/pressure/memory``, read as one answer."""
    fields: dict[str, int] = {}
    for line in meminfo.read_text(encoding="ascii", errors="replace").splitlines():
        name, _, rest = line.partition(":")
        number = rest.split()
        if number and number[0].isdigit():
            fields[name.strip()] = int(number[0])  # kB
    available = fields.get("MemAvailable")
    total = fields.get("MemTotal")
    avg10 = _psi_some_avg10(psi)
    if avg10 is None:
        pressure: Pressure = "normal" if available is not None else "unknown"
    elif avg10 >= PSI_CRITICAL:
        pressure = "critical"
    elif avg10 >= PSI_WARN:
        pressure = "warn"
    else:
        pressure = "normal"
    return Headroom(
        available_mb=available // 1024 if available is not None else None,
        total_mb=total // 1024 if total is not None else None,
        pressure=pressure,
        psi_some_avg10=avg10,
        source="proc",
    )


def _psi_some_avg10(psi: Path) -> float | None:
    """``some avg10`` from a PSI file, or ``None`` on a kernel without PSI (or with it off)."""
    try:
        text = psi.read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    found = re.search(r"^some\b.*?\bavg10=([0-9.]+)", text, re.MULTILINE)
    return float(found.group(1)) if found else None


def darwin_headroom(
    sysctl: Callable[[str], str | None], vm_stat: Callable[[], str | None]
) -> Headroom:
    """macOS: the kernel's pressure level, ``hw.memsize``, and ``vm_stat``'s available pages."""
    level = sysctl("kern.memorystatus_vm_pressure_level")
    pressure = _DARWIN_LEVELS.get((level or "").strip(), "unknown")
    memsize = (sysctl("hw.memsize") or "").strip()
    total = int(memsize) // (1024 * 1024) if memsize.isdigit() else None
    available = darwin_available_bytes(vm_stat() or "")
    return Headroom(
        available_mb=available // (1024 * 1024) if available is not None else None,
        total_mb=total,
        pressure=pressure,
        psi_some_avg10=None,
        source="darwin",
    )


def darwin_available_bytes(text: str) -> int | None:
    """Free + inactive + speculative pages times the page size ``vm_stat`` names; ``None``
    when the output is not ``vm_stat``'s."""
    size = re.search(r"page size of (\d+) bytes", text)
    if size is None:
        return None
    pages = 0
    for name in ("free", "inactive", "speculative"):
        found = re.search(rf"^Pages {name}:\s+(\d+)\.?\s*$", text, re.MULTILINE)
        if found is None:
            return None
        pages += int(found.group(1))
    return pages * int(size.group(1))


def _sysctl(name: str) -> str | None:
    """What ``sysctl -n <name>`` says, from ``sysctl`` on PATH or where macOS keeps it;
    ``None`` when it cannot be asked or does not answer."""
    sysctl = shutil.which("sysctl") or next(
        (place for place in _SYSCTL if os.access(place, os.X_OK)), None
    )
    if sysctl is None:
        return None
    try:
        completed = subprocess.run(
            [sysctl, "-n", name], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def _vm_stat() -> str | None:
    """``vm_stat``'s output (macOS), or ``None``. Its own environment, less our identity:
    it is a page count, not a session."""
    from aisquare.core import spawn

    binary = shutil.which("vm_stat") or (_VM_STAT if os.access(_VM_STAT, os.X_OK) else None)
    if binary is None:
        return None
    try:
        completed = subprocess.run(
            [binary],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=spawn.untraced_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None
