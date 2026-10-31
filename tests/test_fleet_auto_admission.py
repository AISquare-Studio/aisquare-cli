"""Auto admission: ``max_agents_per_project = "auto"``, the default, admits a spawn unless the
machine is busy, and while it is, holds a spawn into a project already running
``busy_floor`` (3) live agents.

The failing-first case is the owner's box: load5 ~54 on 14 cores, memory pressure normal,
and every spawn admitted. Driven as ``tests/test_fleet_service.py`` drives the fleet (its
fixtures, its in-memory tmux), with the probes faked: the machine is whatever a test says.
"""

from __future__ import annotations

import json
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
from tests.test_fleet_service import FakeTmux, _coder, _second_project, _settings

tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project
later = autostop_suite.later

NOW = datetime(2026, 10, 30, 22, 0, tzinfo=UTC)
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
    assert (defaults.busy_floor, defaults.busy_hold_minutes) == (3, 5.0)
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


def test_a_calm_machine_admits_past_the_floor_with_no_note(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, machine: Machine
) -> None:
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
    assert "admission auto: cpu load 0.14 per CPU (load5 2.0 / 14) (>= 1 per CPU: ok)" in (
        row.detail
    )
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
