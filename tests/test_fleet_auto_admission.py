"""Auto admission: ``max_agents_per_project = "auto"``, the default, admits a spawn unless the
machine is busy, and while it is, holds a spawn into a project already running
``busy_floor`` (3) live agents.

The failing-first case is the owner's box: load5 ~54 on 14 cores, memory pressure normal,
and every spawn admitted. Driven as ``tests/test_fleet_service.py`` drives the fleet (its
fixtures, its in-memory tmux), with the probes faked: the machine is whatever a test says.
"""

from __future__ import annotations

import json
import math
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.cli.ui.views.app_settings import parse_max_agents
from aisquare.core import load, memory, paths
from aisquare.core.config import AppConfig, FleetSettings, WatchSettings, load_config, save_config
from aisquare.core.ids import new_agent_id
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.core.tmux import WindowInfo
from aisquare.models import FleetAgent, ProjectInfo
from aisquare.services import autostop, diagnostics
from aisquare.services import fleet as fleet_service
from aisquare.services import project as project_service
from aisquare.services import settings as settings_service
from aisquare.services import watch as watch_service
from tests import test_fleet_autostop as autostop_suite
from tests import test_fleet_service as fleet_suite
from tests.test_fleet_service import FakeTmux, _coder, _git, _second_project, _settings

tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project
later = autostop_suite.later

NOW = datetime(2026, 10, 30, 22, 0, tzinfo=UTC)
#: The real CPU probe, taken at import, before the suite's ``calm_machine`` replaces it.
REAL_CPU_LOAD = load.cpu_load
#: The owner's box when the task was written: 3.86 per logical CPU.
OWNERS_LOAD = (54.0, 14)


class Machine:
    """The probes, faked: what each signal reads, and how often the load was asked."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.load5, self.cpus = 2.0, 14
        self.psi: float | None = None
        self.pressure = "normal"
        self.available_mb = 65_536
        self.thermal = 0
        self.probes = 0
        monkeypatch.setattr(load, "cpu_load", self._cpu)
        monkeypatch.setattr(load, "thermal", lambda: load.Thermal(self.thermal, "test"))
        monkeypatch.setattr(memory, "headroom", self._memory)

    def _cpu(self) -> load.CpuLoad:
        self.probes += 1
        return load.CpuLoad(self.load5, self.cpus, self.psi)

    def _memory(self) -> memory.Headroom:
        return memory.Headroom(self.available_mb, 131_072, self.pressure, None, "test")  # type: ignore[arg-type]

    def busy(self) -> None:
        self.load5, self.cpus = OWNERS_LOAD

    def calm(self) -> None:
        self.load5, self.cpus, self.psi, self.pressure, self.thermal = 2.0, 14, None, "normal", 0


@pytest.fixture
def machine(monkeypatch: pytest.MonkeyPatch) -> Machine:
    return Machine(monkeypatch)


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    """``fleet``'s clock, moved by a test: ``now[0]``."""
    at = [NOW]
    monkeypatch.setattr(fleet_service, "_now", lambda: at[0])
    return at


@pytest.fixture(autouse=True)
def nudges(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def record(project_id: str, *, reason: str, board: str | None = None) -> bool:
        calls.append(project_id)
        return True

    monkeypatch.setattr(fleet_service, "nudge_manager", record)
    return calls


def _live(project: ProjectInfo) -> list[str]:
    with store_session() as store:
        return [a.label for a in store.fleet_agents(project.id, live_only=True)]


def _three(project: ProjectInfo) -> list[FleetAgent]:
    return [_coder(project) for _ in range(3)]


# --- the config: "auto" and the ints every existing file holds -------------------------------


def test_auto_is_the_default_and_parses_beside_old_ints(tmp_path: Path) -> None:
    assert FleetSettings().max_agents_per_project == "auto"
    assert FleetSettings(max_agents_per_project="auto").max_agents_per_project == "auto"
    assert FleetSettings(max_agents_per_project=6).max_agents_per_project == 6
    assert FleetSettings(max_agents_per_project="6").max_agents_per_project == 6
    with pytest.raises(ValueError):
        FleetSettings(max_agents_per_project="lots")
    defaults = FleetSettings()
    assert (defaults.busy_floor, defaults.busy_hold_minutes, defaults.busy_burst) == (3, 5.0, 2)
    assert (defaults.busy_load_per_cpu, defaults.busy_cpu_psi) == (1.0, 40.0)
    assert (defaults.busy_memory, defaults.busy_thermal) == ("warn", "heavy")


@pytest.mark.parametrize(
    ("text", "loaded"),
    [
        ("config_version = 3\n[fleet]\nmax_agents_per_project = 8\n", 8),
        ("config_version = 3\n[fleet]\nmax_agents_per_project = 4\n", 4),
        ("[fleet]\nmax_agents_per_project = 6\n", 6),
        ('config_version = 3\n[fleet]\nmax_agents_per_project = "auto"\n', "auto"),
        ("config_version = 3\n", "auto"),
        # 0.8.0 wrote every setting, the default 4 too: a value it cannot tell from one
        # nobody chose follows today's default, as every other moved default does.
        ("[fleet]\nmax_agents_per_project = 4\n", "auto"),
    ],
)
def test_an_existing_file_loads_its_int_unchanged(
    tmp_path: Path, text: str, loaded: object
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    assert load_config(path).fleet.max_agents_per_project == loaded


def test_config_set_takes_auto_and_a_number_and_refuses_anything_else() -> None:
    assert settings_service.set_value("fleet.max_agents_per_project", "5") == "5"
    assert load_config().fleet.max_agents_per_project == 5
    assert settings_service.set_value("fleet.max_agents_per_project", "auto") == "auto"
    assert load_config().fleet.max_agents_per_project == "auto"
    assert "max_agents_per_project" not in settings_service.overrides().get("fleet", {})
    with pytest.raises(ValueError):
        settings_service.set_value("fleet.max_agents_per_project", "plenty")
    assert settings_service.set_value("fleet.busy_floor", "2") == "2"


@pytest.mark.parametrize(
    ("raw", "parsed"), [("auto", "auto"), (" AUTO ", "auto"), ("", "auto"), ("7", 7)]
)
def test_the_settings_field_takes_auto_or_a_number(raw: str, parsed: object) -> None:
    assert parse_max_agents(raw) == parsed


@pytest.mark.parametrize(("raw", "why"), [("0", "at least 1"), ("many", "auto or a whole")])
def test_the_settings_field_refuses_anything_else(raw: str, why: str) -> None:
    with pytest.raises(ValueError, match=why):
        parse_max_agents(raw)


# --- held, and why ---------------------------------------------------------------------------


def test_on_the_owners_box_the_fourth_agent_is_held_naming_the_signal_and_the_retry(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    machine: Machine,
    now: list[datetime],
) -> None:
    """The failing-first case: load5 54 on 14 cores admitted every spawn. Three are admitted,
    each with a note; the fourth is refused before any window, with the signal, its value,
    its threshold and the earliest retry."""
    machine.busy()
    admitted = [fleet_service.spawn(project, "coder", worktree=False) for _ in range(3)]
    assert all(
        any(note.startswith("the machine is busy (cpu load 3.86 per CPU") for note in r.notes)
        for r in admitted
    ), [r.notes for r in admitted]
    with pytest.raises(fleet_service.CapacityRefused) as held:
        _coder(project)
    assert held.value.code == "capacity_busy"
    assert held.value.retry_at == NOW + timedelta(minutes=5)
    message = str(held.value)
    assert "is held, nothing started: it runs 3 live agents (busy_floor = 3)" in message
    assert "cpu load 3.86 per CPU (load5 54.0 / 14), busy at >= 1 per CPU" in message
    at = (NOW + timedelta(minutes=5)).astimezone().isoformat(timespec="seconds")
    assert f"retry at {at} or later (in 5m 00s)" in message
    assert len(tmux.spawned) == 3, "nothing started"
    assert _live(project) == ["coder-1", "coder-2", "coder-3"]
    [hold] = fleet_service.holds()
    assert (hold.project_id, hold.held_at, hold.until) == (project.id, NOW, held.value.retry_at)
    assert hold.project_name == project_service.display_name(project), "named as refused"
    log = paths.aisquare_home() / "fleet" / "admission.jsonl"
    [line] = [json.loads(row) for row in log.read_text(encoding="utf-8").splitlines()]
    assert line["outcome"] == "capacity_busy" and line["live"] == 3


@pytest.mark.parametrize(
    ("signal", "make_busy", "said"),
    [
        ("cpu load", lambda m: setattr(m, "load5", 14.0), "cpu load 1.00 per CPU"),
        ("cpu pressure", lambda m: setattr(m, "psi", 40.0), "cpu pressure some avg60 40"),
        ("memory pressure", lambda m: setattr(m, "pressure", "warn"), "memory pressure warn"),
        ("thermal", lambda m: setattr(m, "thermal", 2), "thermal heavy (2), busy at >= heavy"),
    ],
)
def test_each_signal_alone_makes_the_machine_busy(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    machine: Machine,
    now: list[datetime],
    signal: str,
    make_busy: Any,
    said: str,
) -> None:
    """Any one signal: the others read calm. Below the floor every spawn is admitted."""
    make_busy(machine)
    assert [a.label for a in _three(project)] == ["coder-1", "coder-2", "coder-3"]
    with pytest.raises(fleet_service.CapacityRefused, match=re.escape(said)) as held:
        _coder(project)
    assert held.value.code == "capacity_busy"


def test_with_the_burst_guard_off_a_calm_machine_admits_past_the_floor_with_no_note(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    machine: Machine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``busy_burst = 0``: auto has no per-project number at all while the machine is calm."""
    _settings(monkeypatch, busy_burst=0)
    receipts = [fleet_service.spawn(project, "coder", worktree=False) for _ in range(6)]
    assert len(_live(project)) == 6
    assert not any("busy" in note for r in receipts for note in r.notes)
    assert fleet_service.holds() == []


def test_below_the_floor_a_project_is_admitted_even_while_another_is_held(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    tmp_path: Path,
    machine: Machine,
    now: list[datetime],
) -> None:
    machine.busy()
    other = _second_project(tmp_path)
    _three(project)
    with pytest.raises(fleet_service.CapacityRefused):
        _coder(project)
    assert [_coder(other).label for _ in range(3)] == ["coder-1", "coder-2", "coder-3"]
    assert [hold.project_id for hold in fleet_service.holds()] == [project.id]


def test_a_retry_inside_the_hold_is_held_without_probing_and_after_it_probes(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    machine: Machine,
    now: list[datetime],
) -> None:
    machine.busy()
    _three(project)
    with pytest.raises(fleet_service.CapacityRefused):
        _coder(project)
    asked = machine.probes
    machine.calm()  # the machine calms, but the hold stands for its five minutes
    now[0] = NOW + timedelta(minutes=3)
    with pytest.raises(
        fleet_service.CapacityRefused, match=r"read 3m 00s ago; retry at .*\(in 2m 00s\)"
    ) as again:
        _coder(project)
    assert again.value.retry_at == NOW + timedelta(minutes=5), "the same hold, not a new one"
    assert machine.probes == asked, "no probe inside the hold"
    now[0] = NOW + timedelta(minutes=5)
    assert _coder(project).label == "coder-4"
    assert machine.probes == asked + 1, "probed once the hold ran out"
    assert fleet_service.holds() == [], "admitted calm: the hold is forgotten"


def test_a_retry_after_the_hold_that_still_reads_busy_is_held_anew(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    machine: Machine,
    now: list[datetime],
) -> None:
    machine.busy()
    _three(project)
    with pytest.raises(fleet_service.CapacityRefused):
        _coder(project)
    now[0] = NOW + timedelta(minutes=6)
    with pytest.raises(fleet_service.CapacityRefused) as again:
        _coder(project)
    assert again.value.retry_at == NOW + timedelta(minutes=11)


def test_busy_floor_and_hold_minutes_are_settings(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    machine: Machine,
    now: list[datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, busy_floor=1, busy_hold_minutes=2)
    machine.busy()
    _coder(project)
    with pytest.raises(fleet_service.CapacityRefused, match="busy_floor = 1") as held:
        _coder(project)
    assert held.value.retry_at == NOW + timedelta(minutes=2)


def test_a_signal_turned_off_holds_nothing(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    machine: Machine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, busy_load_per_cpu=0)
    machine.busy()
    assert len([_coder(project) for _ in range(5)]) == 5


def test_a_malformed_cpu_pressure_file_never_crashes_a_spawn(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    machine: Machine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #12 (2), through the real CPU probe: a Linux ``/proc/pressure/cpu`` whose
    ``avg60`` is not a number crashed every auto-mode spawn with ``ValueError``."""
    psi = tmp_path / "cpu"
    psi.write_text("some avg10=0.00 avg60=1.2.3 avg300=0.00 total=0\n", encoding="ascii")
    monkeypatch.setattr(load, "PSI_CPU", psi)
    monkeypatch.setattr(load, "cpu_load", REAL_CPU_LOAD)
    monkeypatch.setattr("os.getloadavg", lambda: (0.5, 0.5, 0.5), raising=False)
    monkeypatch.setattr("os.cpu_count", lambda: 8)
    assert [_coder(project).label for _ in range(4)] == [
        "coder-1",
        "coder-2",
        "coder-3",
        "coder-4",
    ]


@pytest.mark.parametrize(
    "recorded",
    [
        '{"held_at": "2026-10-30T22:00:00", "until": "2026-10-30T22:05:00", "signal": "x"}',
        '{"held_at": "2026-10-30T22:00:00+00:00", "until": "2026-10-30T22:05:00", "signal": "x"}',
        '{"held_at": "yesterday", "until": "2026-10-30T22:05:00+00:00", "signal": "x"}',
        '{"held_at": 7, "until": null, "signal": "x"}',
        '["not", "a", "hold"]',
        "not json",
        # Review of #12, round 2: out of range. The tester's tampered store, whose local
        # time is past the calendar; a short hold far in the future; one that ends before
        # it began; one longer than the longest hold.
        '{"held_at": "2026-10-30T22:00:00+00:00", "until": "9999-12-31T23:59:59-12:00", '
        '"signal": "x"}',
        '{"held_at": "9999-12-31T23:50:00+00:00", "until": "9999-12-31T23:55:00+00:00", '
        '"signal": "x"}',
        '{"held_at": "2026-10-30T22:05:00+00:00", "until": "2026-10-30T22:00:00+00:00", '
        '"signal": "x"}',
        '{"held_at": "2026-10-30T22:00:00+00:00", "until": "2027-12-30T22:00:00+00:00", '
        '"signal": "x"}',
    ],
)
def test_a_naive_unparsable_or_out_of_range_hold_is_unreadable_and_crashes_nothing(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    machine: Machine,
    now: list[datetime],
    recorded: str,
) -> None:
    """Review of #12 (3): a hold with a naive time was taken as read, and ``now < until``
    raised ``TypeError`` in the spawn, ``fleet capacity`` and the watcher; round 2: one whose
    ``until`` is out of range crashed the refusal, or would hold the project for good.
    Unreadable, it is no hold: the probes answer, and a busy machine writes a readable hold
    over it."""
    key = "fleet:busy_hold:" + project.id
    _three(project)
    with store_session() as store:
        store.set_meta(key, recorded)
    assert fleet_service.Hold.from_meta(project.id, recorded) is None
    assert fleet_service.holds() == [] and fleet_service.freed_holds(NOW) == []
    assert fleet_service.capacity().holds == ()
    assert _coder(project).label == "coder-4", "calm: admitted, the probes answered"
    machine.busy()
    with pytest.raises(fleet_service.CapacityRefused) as held:
        _coder(project)
    assert held.value.code == "capacity_busy"
    [hold] = fleet_service.holds()
    assert (hold.held_at, hold.until) == (NOW, NOW + timedelta(minutes=5)), "written over it"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("busy_hold_minutes", math.inf),
        ("busy_hold_minutes", math.nan),
        ("busy_hold_minutes", 1e12),
        ("busy_hold_minutes", 365 * 24 * 60 + 1),
        ("busy_hold_minutes", -1),
        ("busy_load_per_cpu", math.inf),
        ("busy_load_per_cpu", math.nan),
        ("busy_load_per_cpu", 1001),
        ("busy_cpu_psi", math.inf),
        ("busy_cpu_psi", 101),
        ("busy_floor", 1001),
        ("busy_floor", 0),
        ("busy_floor", -1),
        ("busy_burst", 1001),
        ("busy_burst", -1),
    ],
)
def test_every_busy_number_is_finite_and_bounded(field: str, value: float) -> None:
    """Review of #12, round 2: ``busy_hold_minutes = inf`` (or ``1e12``) was taken, and every
    held spawn then raised ``OverflowError`` computing its retry instead of refusing."""
    with pytest.raises(ValueError):
        FleetSettings(**{field: value})
    year = FleetSettings(busy_hold_minutes=365 * 24 * 60, busy_cpu_psi=100, busy_floor=1000)
    assert (year.busy_hold_minutes, year.busy_cpu_psi, year.busy_floor) == (525_600, 100, 1000)
    assert (FleetSettings(busy_burst=0).busy_burst, FleetSettings(busy_burst=1000).busy_burst) == (
        0,
        1000,
    )


@pytest.mark.parametrize("value", ["inf", "nan", "1e12"])
def test_config_set_refuses_an_unbounded_hold_and_keeps_the_file(value: str) -> None:
    settings_service.set_value("fleet.busy_hold_minutes", "10")
    with pytest.raises(ValueError):
        settings_service.set_value("fleet.busy_hold_minutes", value)
    assert load_config().fleet.busy_hold_minutes == 10


def test_config_set_refuses_a_busy_floor_of_0_and_keeps_the_file() -> None:
    """Review of #12, round 3: at ``busy_floor = 0`` a project with no agent at all was held,
    where the rule is that a project below the floor is always admitted; ``fleet capacity``
    said a spawn would be admitted, and the watcher took the agent-less held project for
    gone and dropped its hold."""
    settings_service.set_value("fleet.busy_floor", "2")
    with pytest.raises(ValueError):
        settings_service.set_value("fleet.busy_floor", "0")
    assert load_config().fleet.busy_floor == 2
    assert settings_service.set_value("fleet.busy_floor", "1") == "1"


def test_a_floor_of_0_set_without_validation_still_admits_a_project_with_no_agent(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    machine: Machine,
    now: list[datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second guard: the floor is read as at least 1, so the first agent of a project is
    admitted however busy the machine, and the second is held."""
    config = _settings(monkeypatch)
    config.busy_floor = 0  # an assignment pydantic does not validate
    machine.busy()
    assert _coder(project).label == "coder-1"
    with pytest.raises(fleet_service.CapacityRefused, match="busy_floor = 1") as held:
        _coder(project)
    assert held.value.code == "capacity_busy"
    assert fleet_service.capacity().busy_floor == 1


def test_the_held_row_names_a_project_the_store_has_no_row_for(
    machine: Machine, now: list[datetime], runner: CliRunner
) -> None:
    """A hold whose project has no store row (an agent-less project held at floor 0, by the
    build before the floor was bounded) is shown by the name its refusal used, not its id;
    one an older build wrote without a name falls back to the id."""
    paths.ensure_home()
    named = fleet_service.Hold(
        "prj_nowhere_named", NOW, NOW + timedelta(minutes=5), "cpu load 9 per CPU", "alpha"
    )
    unnamed = json.dumps(
        {
            "held_at": NOW.isoformat(),
            "until": (NOW + timedelta(minutes=5)).isoformat(),
            "signal": "x",
        }
    )
    with store_session() as store:
        store.set_meta("fleet:busy_hold:prj_nowhere_named", named.as_meta())
        store.set_meta("fleet:busy_hold:prj_nowhere_bare", unnamed)
    shown = dict(fleet_service.capacity().holds)
    assert set(shown) == {"alpha", "prj_nowhere_"}
    assert shown["alpha"].project_name == "alpha"
    lines = runner.invoke(app, ["fleet", "capacity"]).stdout.splitlines()
    assert any(line.startswith("held:     alpha, retry in ") for line in lines), lines
    assert not any("prj_nowhere_named" in line for line in lines), lines


@pytest.mark.parametrize(
    ("minutes", "held_for"),
    [(math.inf, 525_600), (1e12, 525_600), (math.nan, 5), (-5.0, 0)],
)
def test_a_hold_past_the_bounds_is_clamped_and_still_refuses(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    machine: Machine,
    now: list[datetime],
    monkeypatch: pytest.MonkeyPatch,
    minutes: float,
    held_for: float,
) -> None:
    """The second guard, for a settings object changed without validation: clamped to a year
    (``nan``: the default), the held spawn is the refusal it should be, not an
    ``OverflowError``."""
    config = _settings(monkeypatch)
    config.busy_hold_minutes = minutes  # an assignment pydantic does not validate
    machine.busy()
    _three(project)
    with pytest.raises(fleet_service.CapacityRefused) as held:
        _coder(project)
    assert held.value.code == "capacity_busy"
    assert held.value.retry_at == NOW + timedelta(minutes=held_for)
    [hold] = fleet_service.holds()
    assert hold.until == NOW + timedelta(minutes=held_for), "and it reads back as a hold"


def test_json_fleet_spawn_says_capacity_busy_even_with_an_infinite_hold(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    machine: Machine,
    now: list[datetime],
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tester's repro through the real CLI: with the hold at ``inf`` the held spawn exited
    1 with an EMPTY stdout under ``--json``, so a manager parsing it got nothing."""
    config = _settings(monkeypatch)
    config.busy_hold_minutes = math.inf
    machine.busy()
    _three(project)
    monkeypatch.chdir(project.root)
    result = runner.invoke(app, ["--json", "fleet", "spawn", "coder", "--no-worktree"])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["error"] == "capacity_busy"
    assert "cpu load 3.86 per CPU" in payload["detail"]
    retry = (NOW + timedelta(minutes=525_600)).isoformat(timespec="seconds")
    assert payload["hint"] == f"retry_at {retry}"


# --- the hard floors and numeric mode --------------------------------------------------------


def test_the_hard_floors_refuse_below_the_busy_floor(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    tmp_path: Path,
    machine: Machine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """min_free_mb, critical pressure, max_agents_total and a group's max_agents refuse the
    first agent of a project too: auto admission's floor is no way past them."""
    _settings(monkeypatch, min_free_mb=2048)
    machine.available_mb = 1500
    with pytest.raises(fleet_service.CapacityRefused) as low:
        _coder(project)
    assert low.value.code == "capacity_memory"
    machine.available_mb, machine.pressure = 9000, "critical"
    with pytest.raises(fleet_service.CapacityRefused, match="pressure is critical") as hot:
        _coder(project)
    assert hot.value.code == "capacity_memory"
    machine.pressure = "normal"
    _settings(monkeypatch, max_agents_total=1)
    _coder(_second_project(tmp_path))
    with pytest.raises(fleet_service.CapacityRefused) as budget:
        _coder(project)
    assert budget.value.code == "capacity_fleet"
    _settings(monkeypatch)
    from aisquare.services import parent as parent_service

    monkeypatch.setattr(
        parent_service, "cap_refusal", lambda store, pid: "the group is at max_agents = 1"
    )
    with pytest.raises(fleet_service.CapacityRefused) as group:
        _coder(project)
    assert group.value.code == "capacity_group"
    assert _live(project) == [] and fleet_service.holds() == []


@pytest.mark.parametrize("busy", [False, True])
def test_a_number_keeps_todays_behaviour_and_never_probes_the_load(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    machine: Machine,
    monkeypatch: pytest.MonkeyPatch,
    busy: bool,
) -> None:
    _settings(monkeypatch, max_agents_per_project=4)
    if busy:
        machine.busy()
    receipts = [fleet_service.spawn(project, "coder", worktree=False) for _ in range(4)]
    with pytest.raises(fleet_service.CapacityRefused) as refused:
        _coder(project)
    assert refused.value.code == "capacity_project"
    assert "already runs 4 agents (max_agents_per_project = 4)" in str(refused.value)
    assert machine.probes == 0, "a number never asks the load"
    assert not any("busy" in note for r in receipts for note in r.notes)
    assert fleet_service.holds() == []


def test_a_number_is_never_held_by_the_burst_guard(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    machine: Machine,
    now: list[datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``busy_burst`` is auto admission's: a number admits up to itself at once, past
    ``busy_floor + busy_burst``, and records no admit."""
    _settings(monkeypatch, max_agents_per_project=8)
    assert [_coder(project).label for _ in range(8)][-1] == "coder-8"
    with pytest.raises(fleet_service.CapacityRefused) as refused:
        _coder(project)
    assert refused.value.code == "capacity_project"
    with store_session() as store:
        assert store.ui_state(fleet_service._BUSY_BURST_KEY + project.id) is None
    assert fleet_service.holds() == [] and machine.probes == 0


# --- never blocks; races; replacements; auto-stop --------------------------------------------


def test_a_held_spawn_answers_at_once_and_never_waits(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    machine: Machine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The spawn's caller is never blocked: a hold is a refusal, not a wait for the five
    minutes. Nothing sleeps, and the answer comes in well under a second."""
    machine.busy()
    _three(project)

    def no_wait(seconds: float) -> None:
        raise AssertionError(f"a held spawn slept {seconds}s")

    monkeypatch.setattr(fleet_service, "_sleep", no_wait)
    monkeypatch.setattr(autostop, "_sleep", no_wait)
    monkeypatch.setattr(time, "sleep", no_wait)
    started = time.monotonic()
    for _ in range(3):
        with pytest.raises(fleet_service.CapacityRefused):
            _coder(project)
    assert time.monotonic() - started < 5.0
    assert len(tmux.spawned) == 3


def test_a_spawn_that_races_past_the_floor_while_busy_backs_its_row_out(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    machine: Machine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two spawns at 2 live both pass the floor; the row settled third stays, the fourth
    backs out with ``capacity_busy``, as the numeric cap settles its race."""
    machine.busy()
    _coder(project)
    _coder(project)
    original = tmux.spawn_window

    def spawn_then_race(*args: object, **kwargs: object) -> WindowInfo:
        window = original(*args, **kwargs)  # type: ignore[arg-type]
        with store_session() as store:  # a parallel spawn here takes the last seat
            store.upsert_fleet_agent(
                FleetAgent(
                    id=new_agent_id(),
                    project_id=project.id,
                    label="coder-racer",
                    role="coder",
                    pane_id="%99",
                    cwd=project.root,
                    created_at=datetime.now(tz=UTC) - timedelta(seconds=1),
                )
            )
        return window

    monkeypatch.setattr(tmux, "spawn_window", spawn_then_race)
    with pytest.raises(fleet_service.CapacityRefused, match="busy_floor = 3") as raced:
        _coder(project)
    assert raced.value.code == "capacity_busy"
    assert tmux.killed == ["%3"]
    assert len(_live(project)) == 3
    # Review of #12, round 4: held like any other held spawn, with a retry time (the JSON
    # hint) and a hold, so a retry inside it needs no probe and the watcher wakes for it.
    [hold] = fleet_service.holds()
    assert raced.value.retry_at is not None and raced.value.retry_at == hold.until
    assert hold.until - hold.held_at == timedelta(minutes=5)
    assert hold.project_id == project.id and "cpu load 3.86 per CPU" in hold.signal
    assert hold.project_name == project_service.display_name(project)
    assert "retry at " in str(raced.value) and "`aisquare watch` wakes" in str(raced.value)
    monkeypatch.setattr(tmux, "spawn_window", original)
    asked = machine.probes
    with pytest.raises(fleet_service.CapacityRefused) as again:
        _coder(project)
    assert again.value.retry_at == hold.until and machine.probes == asked, "no probe"
    fleet_service.stop(project, "coder-1", force=True)
    [freed] = fleet_service.freed_holds()
    assert freed.project.id == project.id and freed.why.startswith("2 live agents")


def test_two_checkouts_named_alike_are_matched_by_project_id_not_by_name(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    tmp_path: Path,
    machine: Machine,
    now: list[datetime],
    runner: CliRunner,
) -> None:
    """Review of #12, round 4 (the tester's repro): two un-nicknamed checkouts, both
    ``repo``, are two projects, labelled with their roots. The hold in force on A was named
    ``repo`` and matched by that name, which no label is: ``fleet capacity`` said a spawn
    would be admitted while the spawn into A was held. Matched by project id, A is held and
    B is not, everywhere."""
    elsewhere = tmp_path / "elsewhere" / "repo"
    elsewhere.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=elsewhere)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=elsewhere)
    other = team_project(elsewhere)
    with store_session() as store:
        store.onboard_project(other)
    assert project_service.display_name(project) == project_service.display_name(other)
    machine.busy()
    _three(project)
    _coder(other)
    with pytest.raises(fleet_service.CapacityRefused):
        _coder(project)
    machine.calm()
    now[0] = NOW + timedelta(minutes=1)
    with pytest.raises(fleet_service.CapacityRefused) as still:
        _coder(project)
    assert still.value.code == "capacity_busy", "the hold in force, no probe"

    report = fleet_service.capacity()
    a, b = f"repo ({project.root})", f"repo ({other.root})"
    assert report.live == {a: 3, b: 1}
    assert (report.held(a), report.held(b)) == (True, False)
    assert report.reason.endswith(f"held in {a}"), report.reason
    assert [(name, hold.project_id) for name, hold in report.holds] == [(a, project.id)]
    assert report.projects == {a: project.id, b: other.id}

    lines = runner.invoke(app, ["fleet", "capacity"]).stdout.splitlines()
    assert f"  {a}: 3 — held" in lines and f"  {b}: 1" in lines, lines
    assert any(line.startswith(f"held:     {a}, retry in ") for line in lines), lines
    assert lines[-1].startswith("⚠ ") and lines[-1].endswith(f"held in {a}"), lines[-1]
    shown = json.loads(runner.invoke(app, ["--json", "fleet", "capacity"]).stdout)
    [held] = shown["holds"]
    assert (held["project"], held["project_id"]) == (a, project.id)
    assert held["project"] in shown["live"]
    paths.ensure_home()
    row = diagnostics._check_fleet_capacity()
    assert f"held in {a}" in row.detail, row.detail


def test_a_restart_of_a_running_agent_at_the_floor_is_never_held(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, machine: Machine
) -> None:
    """A live 1:1 replacement frees its own seat as it starts: held after its stop, the
    agent would be lost (review of #323's rule, for the busy hold too)."""
    machine.busy()
    agent = _three(project)[0]
    receipt = fleet_service.restart(project, agent.label)
    assert receipt.was_running is True and receipt.started.ended_at is None
    assert len(_live(project)) == 3


def test_a_held_spawn_stops_the_projects_finished_workers_and_goes_ahead(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, machine: Machine, later: None
) -> None:
    """P2-W1b's (c), for ``capacity_busy``: a finished reviewer's seat is the room."""
    machine.busy()
    done = autostop_suite._worker(project, tmux)
    autostop_suite._result(done)
    _coder(project)
    _coder(project)
    receipt = fleet_service.spawn(project, "coder", worktree=False)
    assert done.label not in _live(project) and receipt.agent.label in _live(project)
    assert any(
        note.startswith(f"stopped finished workers to make room: {done.label}")
        for note in receipt.notes
    ), receipt.notes
    assert fleet_service.holds() == []


def test_with_auto_stop_off_the_hold_stands(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, machine: Machine, later: None
) -> None:
    config = load_config()
    config.fleet.auto_stop_finished = False
    save_config(config)
    machine.busy()
    done = autostop_suite._worker(project, tmux)
    autostop_suite._result(done)
    _coder(project)
    _coder(project)
    with pytest.raises(fleet_service.CapacityRefused) as held:
        _coder(project)
    assert held.value.code == "capacity_busy" and done.label in _live(project)


# --- fleet capacity, the doctor, the CLI's refusal ------------------------------------------


def test_fleet_capacity_lists_each_signal_with_value_threshold_and_verdict(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    machine: Machine,
    now: list[datetime],
    runner: CliRunner,
) -> None:
    machine.busy()
    _three(project)
    with pytest.raises(fleet_service.CapacityRefused):
        _coder(project)
    shown = runner.invoke(app, ["fleet", "capacity"])
    assert shown.exit_code == 0, shown.output
    lines = shown.stdout.splitlines()
    name = project_service.display_name(project)
    assert f"  {name}: 3 — held" in lines
    assert any(line.startswith("load:     auto admission") for line in lines)
    [cpu] = [line for line in lines if line.strip().startswith("cpu load")]
    assert " ".join(cpu.split()) == "cpu load 3.86 per CPU (load5 54.0 / 14) >= 1 per CPU busy"
    [mem] = [line for line in lines if line.strip().startswith("memory pressure")]
    assert " ".join(mem.split()) == "memory pressure normal >= warn ok"
    [heat] = [line for line in lines if line.strip().startswith("thermal")]
    assert " ".join(heat.split()) == "thermal nominal (0) >= heavy ok"
    assert any(line.startswith(f"held:     {name}, retry in ") for line in lines)
    assert lines[-1].startswith("⚠ the machine is busy (cpu load 3.86 per CPU")
    assert lines[-1].endswith(f"held in {name}")

    report = json.loads(runner.invoke(app, ["--json", "fleet", "capacity"]).stdout)
    assert report["max_agents_per_project"] == "auto" and report["busy"] is True
    assert report["signals"][0] == {
        "name": "cpu load",
        "value": "3.86 per CPU (load5 54.0 / 14)",
        "threshold": ">= 1 per CPU",
        "verdict": "busy",
    }
    assert [hold["project"] for hold in report["holds"]] == [name]


def test_fleet_capacity_in_numeric_mode_says_the_load_is_not_consulted(
    machine: Machine, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings(monkeypatch, max_agents_per_project=6)
    machine.busy()
    lines = runner.invoke(app, ["fleet", "capacity"]).stdout.splitlines()
    assert "load:     not consulted (max_agents_per_project = 6)" in lines
    assert lines[-1] == "✓ a spawn would be admitted"


def test_the_doctor_capacity_row_lists_each_signal_and_says_busy_without_warning(
    machine: Machine,
) -> None:
    """Each signal with its value, threshold and verdict. A busy CPU is said, not warned of:
    it comes and goes, and ``install`` would list it on every busy box. Memory at warn still
    warns, as it did."""
    paths.ensure_home()
    row = diagnostics._check_fleet_capacity()
    assert row.status.value == "ok"
    assert (
        "admission auto, busy_burst 2 per 5 min: cpu load 0.14 per CPU (load5 2.0 / 14) "
        "(>= 1 per CPU: ok)"
    ) in row.detail
    assert "memory pressure normal (>= warn: ok)" in row.detail
    assert "thermal nominal (0) (>= heavy: ok)" in row.detail
    machine.busy()
    row = diagnostics._check_fleet_capacity()
    assert row.status.value == "ok" and "(>= 1 per CPU: busy)" in row.detail
    assert "the machine is busy (cpu load 3.86 per CPU" in row.detail
    machine.pressure = "warn"
    row = diagnostics._check_fleet_capacity()
    assert row.status.value == "warn" and "busy_*" in (row.fix or "")


def test_a_held_spawn_on_the_cli_is_capacity_busy_with_its_retry_in_json(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    until = NOW + timedelta(minutes=5)

    def held(*args: object, **kwargs: object) -> Any:
        raise fleet_service.CapacityRefused("capacity_busy", "alpha is held", retry_at=until)

    monkeypatch.setattr("aisquare.cli.fleet.fleet_service.spawn", held)
    monkeypatch.setattr(
        "aisquare.cli.fleet.fleet_service.resolve_project",
        lambda ref=None: ProjectInfo(id="prj_x", root=Path.cwd()),
    )
    machine = runner.invoke(app, ["--json", "fleet", "spawn", "coder"])
    payload = json.loads(machine.stdout)
    assert payload["error"] == "capacity_busy" and payload["detail"] == "alpha is held"
    assert payload["hint"] == "retry_at 2026-10-30T22:05:00+00:00"


# --- the burst guard: busy_burst new agents per busy_hold_minutes above the floor -----------


def _ledger(project: ProjectInfo) -> list[str]:
    """The project's recorded admits above the floor, as ``[at, id]`` pairs' times."""
    with store_session() as store:
        text = store.ui_state(fleet_service._BUSY_BURST_KEY + project.id)
    return [at for at, _ in json.loads(text)] if text else []


def test_a_calm_machine_admits_busy_burst_past_the_floor_then_holds_the_next(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    machine: Machine,
    now: list[datetime],
) -> None:
    """REVIEW-485 M2: with the machine calm, auto had no per-project number, and a fan-out was
    admitted in full before load5 moved. The floor's 3 and then ``busy_burst`` (2) more are
    admitted; the next is held like a busy one, until the oldest admit above the floor is
    ``busy_hold_minutes`` old."""
    _three(project)
    now[0] = NOW + timedelta(minutes=1)
    _coder(project)
    now[0] = NOW + timedelta(minutes=2)
    _coder(project)
    assert len(_live(project)) == 5
    assert _ledger(project) == [
        (NOW + timedelta(minutes=1)).isoformat(),
        (NOW + timedelta(minutes=2)).isoformat(),
    ], "the floor's three are not counted"
    now[0] = NOW + timedelta(minutes=3)
    with pytest.raises(fleet_service.CapacityRefused) as held:
        _coder(project)
    assert held.value.code == "capacity_busy"
    assert held.value.retry_at == NOW + timedelta(minutes=6), "the oldest admit ages out"
    message = str(held.value)
    assert "is held, nothing started: it runs 5 live agents (busy_floor = 3)" in message
    assert "burst: 2 agents started above busy_floor in the last 5 min, busy_burst = 2" in message
    assert "busy_burst = 0 in [fleet] turns the guard off" in message
    assert len(tmux.spawned) == 5, "nothing started"
    [hold] = fleet_service.holds()
    assert hold.burst and hold.until == held.value.retry_at and hold.signal.startswith("burst")
    now[0] = NOW + timedelta(minutes=5)
    with pytest.raises(fleet_service.CapacityRefused):
        _coder(project)
    now[0] = NOW + timedelta(minutes=6)
    assert _coder(project).label == "coder-6", "one admit aged out: one seat"
    assert fleet_service.holds() == [], "admitted: the hold is forgotten"
    with pytest.raises(fleet_service.CapacityRefused) as again:
        _coder(project)
    assert again.value.retry_at == NOW + timedelta(minutes=7)


def test_below_the_floor_a_project_is_admitted_while_its_burst_is_full(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    machine: Machine,
    now: list[datetime],
) -> None:
    agents = [_coder(project) for _ in range(5)]
    with pytest.raises(fleet_service.CapacityRefused):
        _coder(project)
    for agent in agents[:3]:
        fleet_service.stop(project, agent.label, force=True)
    assert len(_live(project)) == 2
    _coder(project)
    assert len(_ledger(project)) == 2, "admitted under the floor: not counted"


def test_the_burst_counts_per_project(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    tmp_path: Path,
    machine: Machine,
    now: list[datetime],
) -> None:
    other = _second_project(tmp_path)
    for _ in range(5):
        _coder(project)
    with pytest.raises(fleet_service.CapacityRefused):
        _coder(project)
    assert len([_coder(other) for _ in range(5)]) == 5


def test_a_restart_above_the_floor_is_never_held_and_counts_no_admit(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    machine: Machine,
    now: list[datetime],
) -> None:
    """A live 1:1 replacement takes the seat it frees: no new agent, no admit."""
    agents = [_coder(project) for _ in range(5)]
    receipt = fleet_service.restart(project, agents[-1].label)
    assert receipt.was_running is True and receipt.started.ended_at is None
    assert len(_live(project)) == 5 and len(_ledger(project)) == 2


def test_spawns_that_race_into_the_burst_back_their_row_out(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    machine: Machine,
    now: list[datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two spawns at 4 live, one admit recorded, both find room; the one whose admit is
    written second backs out with ``capacity_busy`` and a burst hold, as the floor settles."""
    for _ in range(4):
        _coder(project)
    original = tmux.spawn_window

    def spawn_then_race(*args: object, **kwargs: object) -> WindowInfo:
        window = original(*args, **kwargs)  # type: ignore[arg-type]
        key = fleet_service._BUSY_BURST_KEY + project.id
        with store_session() as store:  # a parallel spawn records its admit here
            text = store.ui_state(key)
            admits = [*json.loads(text or "[]"), [NOW.isoformat(), "agt_racer"]]
            assert store.replace_ui_state(key, text, json.dumps(admits))
        return window

    monkeypatch.setattr(tmux, "spawn_window", spawn_then_race)
    with pytest.raises(fleet_service.CapacityRefused, match="busy_burst = 2") as raced:
        _coder(project)
    assert raced.value.code == "capacity_busy"
    assert tmux.killed == ["%5"] and len(_live(project)) == 4
    [hold] = fleet_service.holds()
    assert hold.burst and raced.value.retry_at == hold.until == NOW + timedelta(minutes=5)
    assert len(_ledger(project)) == 2, "the backed-out row records no admit"


def test_an_unreadable_or_far_off_ledger_never_holds_for_good(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    machine: Machine,
    now: list[datetime],
) -> None:
    _three(project)
    key = fleet_service._BUSY_BURST_KEY + project.id
    far = (NOW + timedelta(days=365)).isoformat()
    for text in ("not json", json.dumps({"a": 1}), json.dumps([[far, "x"], [far, "y"]])):
        with store_session() as store:
            store.set_ui_state(key, text)
        agent = _coder(project)  # the fourth: above the floor, nothing read as an admit
        assert agent.ended_at is None
        assert all(at <= NOW.isoformat() for at in _ledger(project)), "the far ones dropped"
        fleet_service.stop(project, agent.label, force=True)


def test_fleet_capacity_and_the_doctor_say_the_burst(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    machine: Machine,
    now: list[datetime],
    runner: CliRunner,
) -> None:
    for _ in range(5):
        _coder(project)
    lines = runner.invoke(app, ["fleet", "capacity"]).stdout.splitlines()
    name = project_service.display_name(project)
    assert (
        "burst:    at most 2 new agents per project per 5 min above busy_floor (busy_burst)"
    ) in lines
    assert f"  {name}: 5 — held" in lines
    assert any(
        line.startswith(f"held:     {name}, retry in ") and "(burst: 2 agents started" in line
        for line in lines
    ), lines
    assert lines[-1] == (
        f"⚠ a spawn would be admitted, held in {name}: 2 agents started above busy_floor = 3 "
        "in the last 5 min (busy_burst = 2)"
    )
    report = json.loads(runner.invoke(app, ["--json", "fleet", "capacity"]).stdout)
    assert (report["busy"], report["busy_burst"], report["busy_hold_minutes"]) == (False, 2, 5.0)
    [burst] = report["bursts"]
    assert (burst["project"], burst["project_id"], burst["started"]) == (name, project.id, 2)
    assert burst["retry_at"] == (NOW + timedelta(minutes=5)).isoformat(timespec="seconds")
    row = diagnostics._check_fleet_capacity()
    assert row.status.value == "ok", "a full burst is said, not warned of"
    assert "admission auto, busy_burst 2 per 5 min" in row.detail
    assert f"held in {name}: 2 agents started above busy_floor = 3" in row.detail


def test_fleet_capacity_says_when_the_burst_guard_is_off(
    machine: Machine, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings(monkeypatch, busy_burst=0)
    lines = runner.invoke(app, ["fleet", "capacity"]).stdout.splitlines()
    assert (
        "burst:    off (busy_burst = 0): no per-project limit while the machine is calm"
    ) in lines


def test_watch_wakes_once_a_burst_hold_ages_out(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    machine: Machine,
    now: list[datetime],
    nudges: list[str],
) -> None:
    fleet_service.spawn(project, "manager")
    for _ in range(4):
        _coder(project)
    with pytest.raises(fleet_service.CapacityRefused):
        _coder(project)
    watcher = _watcher()
    assert watcher.tick(NOW + timedelta(minutes=4)).new == [], "the burst is full"
    machine.busy()
    assert watcher.tick(NOW + timedelta(minutes=5)).new == [], "room, but the machine is busy"
    machine.calm()
    nudges.clear()
    [item] = watcher.tick(NOW + timedelta(minutes=6)).new
    assert item.kind == "capacity_freed" and item.summary == (
        "a held spawn may start: fewer than busy_burst = 2 agents started above busy_floor in "
        "the last 5 min — retry it"
    )
    assert str(item.detail["signal"]).startswith("burst: 2 agents")
    assert nudges == [project.id] and fleet_service.holds() == []
    assert watcher.tick(NOW + timedelta(minutes=7)).new == [], "one wake per hold"


# --- aisquare watch wakes the manager when a held project frees up ---------------------------


def _watcher() -> watch_service.Watcher:
    return watch_service.Watcher(
        WatchSettings(auto_stop=False), needs_sources=lambda: _no_needs(), stale=False
    )


def _no_needs() -> Any:
    from tests.test_remote_needs import Fleet, _sources

    return _sources(Fleet(agents=[]))


def _watch_events(project_id: str) -> list[Any]:
    with store_session() as store:
        return [e for e in store.recent_events(project_id, limit=50) if e.kind == "watch"]


def test_watch_wakes_the_manager_once_when_a_held_projects_seat_frees_up(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    machine: Machine,
    now: list[datetime],
    nudges: list[str],
) -> None:
    machine.busy()
    fleet_service.spawn(project, "manager")
    _coder(project)
    _coder(project)
    with pytest.raises(fleet_service.CapacityRefused):
        _coder(project)
    watcher = _watcher()
    quiet = watcher.tick(NOW + timedelta(minutes=1))
    assert quiet.new == [] and _watch_events(project.id) == [], "held, nothing freed"

    fleet_service.stop(project, "coder-2", force=True)
    nudges.clear()  # the stop's own wake
    woke = watcher.tick(NOW + timedelta(minutes=2))

    [item] = woke.new
    assert item.kind == "capacity_freed" and item.project_id == project.id
    assert item.summary == "a held spawn may start: 2 live agents, below busy_floor = 3 — retry it"
    assert "cpu load 3.86 per CPU" in str(item.detail["signal"])
    [event] = _watch_events(project.id)
    assert event.to_role == "manager"
    assert event.text.startswith("watch: 1 new (1 capacity_freed): aisquare watch show ")
    assert nudges == [project.id]
    assert fleet_service.holds() == [], "woken for: the hold is forgotten"
    again = watcher.tick(NOW + timedelta(minutes=3))
    assert again.new == [] and len(_watch_events(project.id)) == 1, "one wake per hold"


def test_watch_wakes_once_the_hold_ran_out_and_the_machine_calmed(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    machine: Machine,
    now: list[datetime],
) -> None:
    machine.busy()
    _three(project)
    with pytest.raises(fleet_service.CapacityRefused):
        _coder(project)
    watcher = _watcher()
    machine.calm()
    assert watcher.tick(NOW + timedelta(minutes=4)).new == [], "inside the hold: no wake"
    machine.busy()
    assert watcher.tick(NOW + timedelta(minutes=6)).new == [], "past it, still busy"
    machine.calm()
    [item] = watcher.tick(NOW + timedelta(minutes=7)).new
    assert item.kind == "capacity_freed" and "the machine is no longer busy" in item.summary
    assert fleet_service.holds() == []


def test_a_watch_delivery_that_fails_keeps_the_hold_for_the_next_tick(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    machine: Machine,
    now: list[datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine.busy()
    _three(project)
    with pytest.raises(fleet_service.CapacityRefused):
        _coder(project)
    fleet_service.stop(project, "coder-1", force=True)
    real = watch_service._deliver

    def broken(*args: Any) -> bool:
        raise RuntimeError("store busy")

    monkeypatch.setattr(watch_service, "_deliver", broken)
    watcher = _watcher()
    assert len(watcher.tick(NOW + timedelta(minutes=1)).new) == 1
    assert len(fleet_service.holds()) == 1, "not delivered: kept"
    monkeypatch.setattr(watch_service, "_deliver", real)
    assert len(watcher.tick(NOW + timedelta(minutes=2)).new) == 1
    assert fleet_service.holds() == []


def test_switching_to_a_number_frees_every_hold(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    machine: Machine,
    now: list[datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine.busy()
    _three(project)
    with pytest.raises(fleet_service.CapacityRefused):
        _coder(project)
    _settings(monkeypatch, max_agents_per_project=8)
    [freed] = fleet_service.freed_holds(NOW + timedelta(minutes=1))
    assert freed.why == "max_agents_per_project = 8: auto admission holds nothing now"
    assert _coder(project).label == "coder-4"


def test_a_saved_auto_config_reaches_spawn(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, machine: Machine
) -> None:
    """Through the real settings read, not a patched one: a file's "auto" holds."""
    config = AppConfig()
    config.fleet.max_agents_per_project = "auto"
    config.fleet.min_free_mb = 0
    save_config(config)
    machine.busy()
    _three(project)
    with pytest.raises(fleet_service.CapacityRefused) as held:
        _coder(project)
    assert held.value.code == "capacity_busy"
