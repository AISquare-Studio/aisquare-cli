"""How busy this machine is: the probes behind ``max_agents_per_project = "auto"``.

A fixed per-project number knew nothing of the box: with the load average at 54 on 14
cores and memory pressure reading normal, every spawn was admitted. In auto mode a spawn
into a project already running ``busy_floor`` agents is held while any one signal here
reads busy (``services.fleet``):

- **CPU load**: the 5-minute load average over the logical CPUs (``os.getloadavg``,
  ``os.cpu_count``), busy at ``busy_load_per_cpu``. On Linux, also ``/proc/pressure/cpu``
  when the kernel has it: ``some avg60``, the share of the last minute in which a runnable
  task waited for a CPU, busy at ``busy_cpu_psi``.
- **Memory**: :func:`aisquare.core.memory.headroom`'s pressure, busy at ``busy_memory``.
- **Thermal**: on macOS, the thermal pressure level the kernel posts
  (``notifyutil -g com.apple.system.thermalpressurelevel``: 0 nominal, 1 moderate,
  2 heavy, 3 trapping, 4 sleeping; no root), busy at ``busy_thermal``. Elsewhere there is
  no cheap source as reliable, and the signal reads ``unknown``.

Never raises: a probe that cannot be read is an ``unknown`` signal, and an unknown signal
is not busy.
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

from aisquare.core import memory

PSI_CPU = Path("/proc/pressure/cpu")
_NOTIFYUTIL = "/usr/bin/notifyutil"
_THERMAL_KEY = "com.apple.system.thermalpressurelevel"

THERMAL_LEVELS = ("nominal", "moderate", "heavy", "trapping", "sleeping")
"""macOS's thermal pressure levels, by the number the kernel posts."""

Verdict = Literal["busy", "ok", "off", "unknown"]
"""A signal against its threshold: ``off`` when its threshold is turned off in config."""


@dataclass(frozen=True)
class CpuLoad:
    """The 5-minute load average, the logical CPUs, and Linux's CPU pressure."""

    load5: float | None
    cpus: int | None
    psi_some_avg60: float | None

    @property
    def per_cpu(self) -> float | None:
        if self.load5 is None or not self.cpus:
            return None
        return self.load5 / self.cpus


@dataclass(frozen=True)
class Thermal:
    """macOS's thermal pressure level (0-4), or ``None`` where it cannot be read."""

    level: int | None
    source: str
    """``notifyutil`` (macOS), or ``unsupported`` / ``unknown``: where the level came from."""

    @property
    def name(self) -> str:
        if self.level is None:
            return "unknown"
        return THERMAL_LEVELS[self.level] if self.level < len(THERMAL_LEVELS) else str(self.level)


@dataclass(frozen=True)
class Thresholds:
    """Where each signal reads busy; ``0`` or ``off`` turns one off (``[fleet] busy_*``)."""

    load_per_cpu: float = 1.0
    cpu_psi: float = 40.0
    memory: Literal["warn", "critical", "off"] = "warn"
    thermal: Literal["moderate", "heavy", "trapping", "sleeping", "off"] = "heavy"


@dataclass(frozen=True)
class Signal:
    """One reading against its threshold, worded for a refusal and for ``fleet capacity``."""

    name: str
    value: str
    threshold: str
    verdict: Verdict

    @property
    def busy(self) -> bool:
        return self.verdict == "busy"

    def said(self) -> str:
        """``cpu load 4.63 per CPU (load5 64.8 / 14), busy at >= 1.0``: a refusal's words."""
        return f"{self.name} {self.value}, busy at {self.threshold}"

    def as_json(self) -> dict[str, str]:
        return {
            "name": self.name,
            "value": self.value,
            "threshold": self.threshold,
            "verdict": self.verdict,
        }


@dataclass(frozen=True)
class Reading:
    """Every signal, read once: busy when any one of them is."""

    signals: tuple[Signal, ...]

    @property
    def busy(self) -> Signal | None:
        """The first signal that reads busy, or ``None``: the one a hold names."""
        return next((signal for signal in self.signals if signal.busy), None)


def cpu_load() -> CpuLoad:
    """This machine's CPU load now. Never raises; ``None`` for what cannot be read."""
    try:
        load5: float | None = os.getloadavg()[1]
    except (AttributeError, OSError):  # Windows has no load average
        load5 = None
    try:
        cpus = os.cpu_count()
    except Exception:  # a count is an answer or unknown, never a crash at spawn
        cpus = None
    return CpuLoad(load5, cpus, psi_some_avg60(PSI_CPU))


def psi_some_avg60(psi: Path) -> float | None:
    """``some avg60`` from a PSI file, or ``None`` on a kernel without PSI (or with it off)."""
    try:
        text = psi.read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    found = re.search(r"^some\b.*?\bavg60=([0-9.]+)", text, re.MULTILINE)
    return float(found.group(1)) if found else None


def thermal() -> Thermal:
    """This machine's thermal pressure now. Never raises."""
    if sys.platform != "darwin":
        return Thermal(None, "unsupported")
    try:
        return darwin_thermal(_notifyutil)
    except Exception:  # a probe is an answer or unknown, never a crash at spawn
        return Thermal(None, "unknown")


def darwin_thermal(notifyutil: Callable[[str], str | None]) -> Thermal:
    """``notifyutil -g com.apple.system.thermalpressurelevel``: the key, then the level."""
    said = notifyutil(_THERMAL_KEY) or ""
    found = re.search(rf"^{re.escape(_THERMAL_KEY)}\s+(\d+)\s*$", said.strip(), re.MULTILINE)
    if found is None:
        return Thermal(None, "unknown")
    return Thermal(int(found.group(1)), "notifyutil")


def _notifyutil(key: str) -> str | None:
    """What ``notifyutil -g <key>`` says (macOS), or ``None`` when it cannot be asked."""
    binary = shutil.which("notifyutil") or (
        _NOTIFYUTIL if os.access(_NOTIFYUTIL, os.X_OK) else None
    )
    if binary is None:
        return None
    try:
        completed = subprocess.run(
            [binary, "-g", key], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def read(thresholds: Thresholds) -> Reading:
    """Every probe, now, against ``thresholds``. Never raises."""
    return evaluate(cpu_load(), memory.headroom(), thermal(), thresholds)


def evaluate(
    load: CpuLoad, head: memory.Headroom, heat: Thermal, thresholds: Thresholds
) -> Reading:
    """The readings against ``thresholds``: one signal each, in the order a hold names them.
    Linux's CPU pressure is listed only where the kernel has it."""
    signals = [_load_signal(load, thresholds.load_per_cpu)]
    if load.psi_some_avg60 is not None:
        signals.append(_psi_signal(load.psi_some_avg60, thresholds.cpu_psi))
    signals.append(_memory_signal(head, thresholds.memory))
    signals.append(_thermal_signal(heat, thresholds.thermal))
    return Reading(tuple(signals))


def _load_signal(load: CpuLoad, at: float) -> Signal:
    threshold = f">= {at:g} per CPU" if at > 0 else "off"
    per_cpu = load.per_cpu
    if per_cpu is None:
        return Signal("cpu load", "cannot be read", threshold, "off" if at <= 0 else "unknown")
    value = f"{per_cpu:.2f} per CPU (load5 {load.load5:.1f} / {load.cpus})"
    return Signal("cpu load", value, threshold, _verdict(per_cpu >= at, on=at > 0))


def _psi_signal(avg60: float, at: float) -> Signal:
    threshold = f">= {at:g}" if at > 0 else "off"
    return Signal(
        "cpu pressure", f"some avg60 {avg60:g}", threshold, _verdict(avg60 >= at, on=at > 0)
    )


_MEMORY_ORDER = {"normal": 0, "warn": 1, "critical": 2}


def _memory_signal(head: memory.Headroom, at: str) -> Signal:
    threshold = f">= {at}" if at != "off" else "off"
    value: str = head.pressure
    if head.psi_some_avg10 is not None:
        value += f" (PSI some avg10 {head.psi_some_avg10:g})"
    if at == "off":
        return Signal("memory pressure", value, threshold, "off")
    if head.pressure == "unknown":
        return Signal("memory pressure", "cannot be read", threshold, "unknown")
    busy = _MEMORY_ORDER[head.pressure] >= _MEMORY_ORDER[at]
    return Signal("memory pressure", value, threshold, "busy" if busy else "ok")


def _thermal_signal(heat: Thermal, at: str) -> Signal:
    threshold = f">= {at}" if at != "off" else "off"
    if at == "off":
        return Signal("thermal", heat.name, threshold, "off")
    if heat.level is None:
        said = "not read on this platform" if heat.source == "unsupported" else "cannot be read"
        return Signal("thermal", said, threshold, "unknown")
    busy = heat.level >= THERMAL_LEVELS.index(at)
    return Signal("thermal", f"{heat.name} ({heat.level})", threshold, "busy" if busy else "ok")


def _verdict(over: bool, *, on: bool) -> Verdict:
    if not on:
        return "off"
    return "busy" if over else "ok"
