"""The machine's load probes behind auto admission: CPU load, Linux's CPU pressure, memory
pressure, macOS's thermal pressure — each against its threshold.

Every case reads fixture values, never this machine: the suite's ``calm_machine`` fixture
stands in for ``cpu_load()`` and ``thermal()`` everywhere else, and these are the readers
behind them.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from aisquare.core import load, memory

#: The real probes, taken at import, before the suite's ``calm_machine`` fixture replaces
#: them for every test.
REAL_CPU_LOAD = load.cpu_load
REAL_THERMAL = load.thermal

CALM = load.CpuLoad(2.0, 8, None)
ROOMY = memory.Headroom(65_536, 131_072, "normal", None, "test")
COOL = load.Thermal(0, "notifyutil")
DEFAULTS = load.Thresholds()


def _verdicts(reading: load.Reading) -> dict[str, str]:
    return {signal.name: signal.verdict for signal in reading.signals}


# --- the readers ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "level", "name"),
    [
        ("com.apple.system.thermalpressurelevel 0\n", 0, "nominal"),
        ("com.apple.system.thermalpressurelevel 1\n", 1, "moderate"),
        ("com.apple.system.thermalpressurelevel 2\n", 2, "heavy"),
        ("com.apple.system.thermalpressurelevel 3\n", 3, "trapping"),
        ("com.apple.system.thermalpressurelevel 4\n", 4, "sleeping"),
    ],
)
def test_notifyutil_reads_each_thermal_level(said: str, level: int, name: str) -> None:
    heat = load.darwin_thermal(lambda key: said)
    assert (heat.level, heat.name, heat.source) == (level, name, "notifyutil")


@pytest.mark.parametrize("said", [None, "", "com.apple.system.other 2\n", "garbage"])
def test_notifyutil_that_says_nothing_readable_is_unknown(said: str | None) -> None:
    heat = load.darwin_thermal(lambda key: said)
    assert (heat.level, heat.name, heat.source) == (None, "unknown", "unknown")


def test_cpu_pressure_reads_some_avg60_and_a_kernel_without_psi_is_none(tmp_path: Path) -> None:
    psi = tmp_path / "cpu"
    psi.write_text(
        "some avg10=12.50 avg60=41.20 avg300=30.00 total=123\n"
        "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n",
        encoding="ascii",
    )
    assert load.psi_some_avg60(psi) == 41.2
    assert load.psi_some_avg60(tmp_path / "missing") is None
    psi.write_text("nothing here\n", encoding="ascii")
    assert load.psi_some_avg60(psi) is None


def test_the_real_cpu_probe_answers_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """``os.getloadavg`` and ``os.cpu_count`` read as they are; a platform without a load
    average (Windows) is ``None``, never a crash at spawn."""
    read = REAL_CPU_LOAD()
    if hasattr(os, "getloadavg"):
        assert read.load5 is not None and read.load5 >= 0
    assert read.cpus is None or read.cpus >= 1

    def no_load() -> tuple[float, float, float]:
        raise OSError("no load average here")

    monkeypatch.setattr(os, "getloadavg", no_load, raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    blind = REAL_CPU_LOAD()
    assert (blind.load5, blind.cpus, blind.per_cpu) == (None, None, None)


def test_the_real_thermal_probe_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off macOS there is no cheap and reliable source: ``unsupported``. On macOS, a
    ``notifyutil`` that breaks is ``unknown``."""
    monkeypatch.setattr(sys, "platform", "linux")
    assert REAL_THERMAL() == load.Thermal(None, "unsupported")

    def broken(key: str) -> str | None:
        raise RuntimeError("notifyutil exploded")

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(load, "_notifyutil", broken)
    assert REAL_THERMAL() == load.Thermal(None, "unknown")


# --- each signal against its threshold ------------------------------------------------------


def test_a_calm_machine_reads_ok_on_every_signal_and_is_not_busy() -> None:
    reading = load.evaluate(CALM, ROOMY, COOL, DEFAULTS)
    assert _verdicts(reading) == {"cpu load": "ok", "memory pressure": "ok", "thermal": "ok"}
    assert reading.busy is None


@pytest.mark.parametrize(("load5", "verdict"), [(13.9, "ok"), (14.0, "busy"), (54.0, "busy")])
def test_cpu_load_is_busy_at_one_per_logical_cpu(load5: float, verdict: str) -> None:
    """The owner's box: load5 ~54 on 14 cores, which today's admission let through."""
    reading = load.evaluate(load.CpuLoad(load5, 14, None), ROOMY, COOL, DEFAULTS)
    [cpu] = [signal for signal in reading.signals if signal.name == "cpu load"]
    assert cpu.verdict == verdict and cpu.threshold == ">= 1 per CPU"
    assert cpu.value == f"{load5 / 14:.2f} per CPU (load5 {load5:.1f} / 14)"
    if verdict == "busy":
        assert reading.busy == cpu
        assert cpu.said() == f"cpu load {cpu.value}, busy at >= 1 per CPU"


def test_cpu_pressure_is_listed_only_where_the_kernel_has_it_and_busy_at_40() -> None:
    names = [s.name for s in load.evaluate(CALM, ROOMY, COOL, DEFAULTS).signals]
    assert "cpu pressure" not in names
    for avg60, verdict in ((39.9, "ok"), (40.0, "busy")):
        reading = load.evaluate(load.CpuLoad(2.0, 8, avg60), ROOMY, COOL, DEFAULTS)
        assert _verdicts(reading)["cpu pressure"] == verdict
    busy = load.evaluate(load.CpuLoad(2.0, 8, 55.0), ROOMY, COOL, DEFAULTS).busy
    assert busy is not None and busy.said() == "cpu pressure some avg60 55, busy at >= 40"


@pytest.mark.parametrize(
    ("pressure", "at", "verdict"),
    [
        ("normal", "warn", "ok"),
        ("warn", "warn", "busy"),
        ("critical", "warn", "busy"),
        ("warn", "critical", "ok"),
        ("critical", "critical", "busy"),
        ("critical", "off", "off"),
        ("unknown", "warn", "unknown"),
    ],
)
def test_memory_pressure_is_busy_at_its_threshold(pressure: str, at: str, verdict: str) -> None:
    head = memory.Headroom(8000, 16_384, pressure, None, "test")  # type: ignore[arg-type]
    thresholds = load.Thresholds(memory=at)  # type: ignore[arg-type]
    assert _verdicts(load.evaluate(CALM, head, COOL, thresholds))["memory pressure"] == verdict


@pytest.mark.parametrize(
    ("level", "at", "verdict"),
    [
        (0, "heavy", "ok"),
        (1, "heavy", "ok"),
        (2, "heavy", "busy"),
        (4, "heavy", "busy"),
        (1, "moderate", "busy"),
        (3, "off", "off"),
        (None, "heavy", "unknown"),
    ],
)
def test_thermal_pressure_is_busy_at_heavy_by_default(
    level: int | None, at: str, verdict: str
) -> None:
    heat = load.Thermal(level, "notifyutil" if level is not None else "unsupported")
    thresholds = load.Thresholds(thermal=at)  # type: ignore[arg-type]
    reading = load.evaluate(CALM, ROOMY, heat, thresholds)
    assert _verdicts(reading)["thermal"] == verdict


def test_a_threshold_of_zero_turns_a_cpu_signal_off() -> None:
    hot = load.CpuLoad(80.0, 8, 90.0)
    reading = load.evaluate(hot, ROOMY, COOL, load.Thresholds(load_per_cpu=0, cpu_psi=0))
    assert _verdicts(reading) == {
        "cpu load": "off",
        "cpu pressure": "off",
        "memory pressure": "ok",
        "thermal": "ok",
    }
    assert reading.busy is None


def test_an_unreadable_signal_is_unknown_and_never_busy() -> None:
    reading = load.evaluate(
        load.CpuLoad(None, None, None), memory.UNKNOWN, load.Thermal(None, "unknown"), DEFAULTS
    )
    assert set(_verdicts(reading).values()) == {"unknown"}
    assert reading.busy is None, "unknown admits"


def test_the_first_busy_signal_is_the_one_a_hold_names() -> None:
    warm = memory.Headroom(8000, 16_384, "warn", None, "test")
    hot = load.Thermal(3, "notifyutil")
    reading = load.evaluate(load.CpuLoad(1.0, 8, None), warm, hot, DEFAULTS)
    busy = reading.busy
    assert busy is not None and busy.name == "memory pressure"
    assert [s.name for s in reading.signals if s.busy] == ["memory pressure", "thermal"]


# --- review of #12: no probe raises -----------------------------------------------------------


@pytest.mark.parametrize("avg60", ["1.2.3", ".", "4.5.6.7"])
def test_a_malformed_cpu_pressure_is_no_reading_and_no_probe_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, avg60: str
) -> None:
    """Review of #12 (2): ``float()`` on ``avg60=1.2.3`` raised ``ValueError`` through
    ``cpu_load`` and ``read``, and a Linux spawn in auto mode crashed. Not a number is no
    reading; and whatever a probe does, ``read`` answers, every signal ``unknown``."""
    psi = tmp_path / "cpu"
    psi.write_text(f"some avg10=0.00 avg60={avg60} avg300=0.00 total=0\n", encoding="ascii")
    assert load.psi_some_avg60(psi) is None
    monkeypatch.setattr(load, "PSI_CPU", psi)
    monkeypatch.setattr(os, "getloadavg", lambda: (1.0, 2.0, 3.0), raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    assert REAL_CPU_LOAD() == load.CpuLoad(2.0, 8, None)

    def broken() -> object:
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(os, "cpu_count", broken)
    assert REAL_CPU_LOAD() == load.UNKNOWN_LOAD
    monkeypatch.setattr(load, "cpu_load", broken)
    monkeypatch.setattr(memory, "headroom", broken)
    monkeypatch.setattr(load, "thermal", broken)
    reading = load.read(DEFAULTS)
    assert set(_verdicts(reading).values()) == {"unknown"} and reading.busy is None
