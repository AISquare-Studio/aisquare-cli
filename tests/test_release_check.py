"""asq's daily release check and the one-click update's record (``services.updates``).

The check runs the real path down to PyPI: ``updates.check_for_release`` →
``lifecycle.latest_release`` → ``install_route.fetch_latest``, which the ``machine`` fake
answers and counts, on the ``tool`` fake's uv tool install of 0.9.0. ``no_real_installer``
closes every way out of the process, so a test that forgot a fake fails rather than asking
PyPI.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aisquare.core.store import store_session
from aisquare.models import FleetAgent, FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import install_route, lifecycle, updates
from aisquare.services import settings as settings_service
from aisquare.services.install_route import LatestRelease
from tests.installer_seams import no_real_installer  # noqa: F401 — autouse, applied by import
from tests.test_lifecycle_upgrade import Machine, Tool, machine, tool  # noqa: F401

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


@pytest.fixture
def running(monkeypatch: pytest.MonkeyPatch, tool: Tool) -> Tool:  # noqa: F811
    """The ``tool`` install, as the version the check compares against too."""
    monkeypatch.setattr(updates, "__version__", "0.9.0")
    return tool


def _state(key: str) -> str | None:
    with store_session() as store:
        return store.ui_state(key)


def _answered_at(when: datetime, latest: str = "0.9.1") -> None:
    with store_session() as store:
        store.set_ui_state(updates.CHECKED_KEY, when.isoformat())
        store.set_ui_state(updates.LATEST_KEY, latest)


# --- cadence ----------------------------------------------------------------------------------


def test_a_first_check_asks_pypi_once_and_offers_the_newer_release(
    running: Tool,
    machine: Machine,  # noqa: F811
) -> None:
    offer = updates.check_for_release(NOW)

    assert machine.lookups == 1
    assert offer == updates.Offer(version="0.9.1", current="0.9.0", runnable=True, announce=True)
    assert _state(updates.CHECKED_KEY) == NOW.isoformat()
    assert _state(updates.LATEST_KEY) == "0.9.1"


@pytest.mark.parametrize("age", [timedelta(0), timedelta(hours=1), timedelta(hours=23, minutes=59)])
def test_no_lookup_while_the_last_answer_is_under_a_day_old(
    running: Tool,
    machine: Machine,  # noqa: F811
    age: timedelta,
) -> None:
    """The answer kept is offered instead: the row stays without asking again."""
    _answered_at(NOW - age)

    offer = updates.check_for_release(NOW)

    assert machine.lookups == 0
    assert offer is not None and offer.version == "0.9.1"


@pytest.mark.parametrize("age", [timedelta(hours=24), timedelta(days=9)])
def test_a_lookup_once_the_last_answer_is_a_day_old_or_more(
    running: Tool,
    machine: Machine,  # noqa: F811
    age: timedelta,
) -> None:
    _answered_at(NOW - age, latest="0.9.1")
    machine.latest = LatestRelease("0.9.2")

    offer = updates.check_for_release(NOW)

    assert machine.lookups == 1
    assert offer is not None and offer.version == "0.9.2"
    assert _state(updates.CHECKED_KEY) == NOW.isoformat()


def test_a_clock_set_back_does_not_hold_the_check_off() -> None:
    assert updates.due(NOW + timedelta(hours=1), NOW) is True
    assert updates.due(NOW - timedelta(hours=1), NOW) is False, "control: an hour old is not due"
    assert updates.due(None, NOW) is True


def test_a_failed_lookup_records_no_check_and_the_next_ask_tries_again(
    running: Tool,
    machine: Machine,  # noqa: F811
) -> None:
    machine.latest = LatestRelease(None, "could not reach PyPI (offline)")

    first = updates.check_for_release(NOW)
    an_hour_later = updates.check_for_release(NOW + timedelta(hours=1))

    assert first is None and an_hour_later is None
    assert machine.lookups == 2, "not counted as a check: the next ask looks up again"
    assert _state(updates.CHECKED_KEY) is None
    assert _state(updates.LATEST_KEY) is None


def test_a_failed_lookup_keeps_offering_what_an_earlier_answer_found(
    running: Tool,
    machine: Machine,  # noqa: F811
) -> None:
    _answered_at(NOW - timedelta(days=2), latest="0.9.1")
    machine.latest = LatestRelease(None, "could not reach PyPI (offline)")

    offer = updates.check_for_release(NOW)

    assert machine.lookups == 1
    assert offer is not None and offer.version == "0.9.1"
    assert _state(updates.CHECKED_KEY) == (NOW - timedelta(days=2)).isoformat(), "not moved"


def test_with_the_opt_out_there_is_never_a_lookup(
    running: Tool,
    machine: Machine,  # noqa: F811
) -> None:
    """``[updates] check = false``, set the way a person sets it; due or not, nothing asked."""
    settings_service.set_value("updates.check", "false")
    _answered_at(NOW - timedelta(days=3))

    assert updates.check_for_release(NOW) is None
    assert updates.check_for_release(NOW + timedelta(days=5)) is None
    assert machine.lookups == 0

    settings_service.set_value("updates.check", "true")
    assert updates.check_for_release(NOW) is not None and machine.lookups == 1, (
        "control: on, it asks"
    )


@pytest.mark.parametrize("kind", [install_route.EDITABLE, install_route.LOCAL_SOURCE])
def test_a_checkout_never_looks_up_and_shows_no_row(
    running: Tool,
    machine: Machine,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    route = install_route.InstallRoute(kind, running.facts, source="/src/aisquare-cli")
    monkeypatch.setattr(install_route, "detect", lambda: route)
    _answered_at(NOW - timedelta(days=3))

    assert updates.check_for_release(NOW) is None
    assert machine.lookups == 0


def test_an_install_asq_does_not_update_still_gets_the_row_with_no_restart(
    running: Tool,
    machine: Machine,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Homebrew: checked and offered; its dialog says the command instead (``runnable``)."""
    route = install_route.InstallRoute(install_route.HOMEBREW, running.facts, formula="aisquare")
    monkeypatch.setattr(install_route, "detect", lambda: route)

    offer = updates.check_for_release(NOW)

    assert machine.lookups == 1
    assert offer is not None and offer.version == "0.9.1" and offer.runnable is False


def test_the_row_says_restart_only_where_the_dialogs_plan_runs(
    running: Tool,
    machine: Machine,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Offer.runnable`` is the plan's ``runnable``, the dialog's own rule: a uv tool whose
    uv is gone from PATH is offered how, not a restart (review of #301)."""
    monkeypatch.setattr(install_route, "find_uv", lambda: None)

    offer = updates.check_for_release(NOW)

    assert offer is not None and offer.runnable is False
    assert lifecycle.upgrade_plan().runnable is False, "the plan agrees"


def test_the_update_is_pinned_to_the_release_offered_and_asks_pypi_nothing(
    running: Tool,
    machine: Machine,  # noqa: F811
) -> None:
    """The version the dialog names is the one installed: a uv tool's plan is pinned to it,
    which needs no lookup, so a newer release out since is not what the press installs."""
    machine.latest = LatestRelease("0.9.2")

    plan = updates.update_plan("0.9.1")

    assert plan.target == "0.9.1" and plan.runnable
    assert plan.argv[-1].endswith("@0.9.1"), plan.argv
    assert machine.lookups == 0


def test_where_brew_cannot_pin_the_update_is_the_newest_and_names_no_number(
    running: Tool,
    machine: Machine,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = install_route.InstallRoute(install_route.HOMEBREW, running.facts, formula="aisquare")
    monkeypatch.setattr(install_route, "detect", lambda: route)

    plan = updates.update_plan("0.9.1")

    assert plan.target is None, "Homebrew installs its formula's version, not one asq names"
    assert plan.command == "brew upgrade aisquare" and not plan.runnable
    assert machine.lookups == 0, "the newest release's plan is built on the check's answer"


# --- what is offered, and announced -----------------------------------------------------------


@pytest.mark.parametrize("latest", ["0.9.0", "0.8.9", "0.9.0rc1"])
def test_no_row_when_the_running_version_equals_or_is_ahead_of_the_latest(
    running: Tool,
    machine: Machine,  # noqa: F811
    latest: str,
) -> None:
    machine.latest = LatestRelease(latest)

    assert updates.check_for_release(NOW) is None
    assert machine.lookups == 1
    assert _state(updates.LATEST_KEY) == latest, "the answer is recorded all the same"


def test_the_row_goes_once_the_version_that_runs_is_the_one_offered(
    running: Tool,
    machine: Machine,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the update: the same record, a newer asq, no row and no lookup."""
    assert updates.check_for_release(NOW) is not None
    monkeypatch.setattr(updates, "__version__", "0.9.1")

    assert updates.check_for_release(NOW + timedelta(minutes=1)) is None
    assert machine.lookups == 1


def test_a_release_is_announced_once_and_a_newer_one_again(
    running: Tool,
    machine: Machine,  # noqa: F811
) -> None:
    first = updates.check_for_release(NOW)
    next_start = updates.check_for_release(NOW + timedelta(hours=1))
    machine.latest = LatestRelease("0.9.2")
    a_day_later = updates.check_for_release(NOW + timedelta(days=1))
    after_that = updates.check_for_release(NOW + timedelta(days=1, hours=1))

    announced = [o.announce if o else None for o in (first, next_start, a_day_later, after_that)]
    assert announced == [True, False, True, False]
    assert [o.version if o else None for o in (first, a_day_later)] == ["0.9.1", "0.9.2"]
    assert _state(updates.ANNOUNCED_KEY) == "0.9.2"


# --- the press and the reopen -----------------------------------------------------------------


def _agent(project: ProjectInfo, label: str, pane: str) -> FleetAgent:
    agent = FleetAgent(
        id=f"agt_{label}",
        project_id=project.id,
        label=label,
        role="coder",
        pane_id=pane,
        tmux_socket="asq-test-release-check",  # never the fleet's real "asq"
        cwd=Path("/w"),
        created_at=NOW,
    )
    with store_session() as store:
        return store.upsert_fleet_agent(agent)


@pytest.fixture
def project(tmp_path: Path) -> ProjectInfo:
    with store_session() as store:
        return store.onboard_project(ProjectInfo(id="prj_release", root=tmp_path / "proj"))


def test_the_press_is_read_back_once_by_the_reopen(project: ProjectInfo) -> None:
    agents = [_agent(project, "coder-1", "%3"), _agent(project, "reviewer-1", "%7")]

    recorded = updates.record_press(agents)
    taken = updates.take_press()

    assert taken == recorded
    assert [(k.agent_id, k.pane_id, k.label) for k in taken.agents] == [
        ("agt_coder-1", "%3", "coder-1"),
        ("agt_reviewer-1", "%7", "reviewer-1"),
    ]
    assert updates.take_press() is None, "forgotten as it is read: one reopen acts on it"


def test_the_press_reads_each_agent_again_and_keeps_only_the_live(
    project: ProjectInfo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the press every live row of the listed projects is read again from tmux: one that
    exited since the frame is not recorded, so the reopen never restarts it (review of #301,
    m1). A row of a project the frame does not list is not asked of at all."""
    with store_session() as store:
        other = store.onboard_project(ProjectInfo(id="prj_other", root=tmp_path / "other"))
    agents = [
        _agent(project, "coder-1", "%1"),
        _agent(project, "coder-2", "%2"),
        _agent(project, "coder-3", "%3"),
        _agent(project, "coder-4", "%4"),
    ]
    _agent(other, "elsewhere-1", "%9")
    fleet = _Fleet(
        monkeypatch,
        {
            "coder-1": ("waiting", ""),
            "coder-2": ("exited", "exit 0"),
            "coder-3": ("lost", "pane gone"),
            "coder-4": ("unknown", "tmux could not be asked"),
        },
    )
    monkeypatch.setattr(updates, "__version__", "0.9.0")

    alive = updates.agents_alive_now({project.id})
    updates.record_press(alive)
    fleet.states.update({"coder-1": ("lost", "pane gone"), "coder-4": ("waiting", "")})
    monkeypatch.setattr(updates, "__version__", "0.9.1")
    outcome = updates.reattach_after_update()

    assert [agent.label for agent in alive] == ["coder-1", "coder-4"]
    assert agents[1].label == "coder-2" and fleet.restarted == [("coder-1", "agt_coder-1")]
    assert outcome is not None
    assert (
        outcome.toast() == "✓ aisquare 0.9.1 — 1 agent reattached, 1 restarted (coder-1: pane gone)"
    )


def test_a_record_that_cannot_be_read_is_no_press() -> None:
    with store_session() as store:
        store.set_ui_state(updates.PRESSED_KEY, "{not json")

    assert updates.take_press() is None
    assert _state(updates.PRESSED_KEY) is None


class _Fleet:
    """``status_of`` per label, and every ``restart`` asked: the fleet the reopen finds."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, states: dict[str, tuple[str, str]]) -> None:
        self.states = states
        self.restarted: list[tuple[str, str | None]] = []
        self.refuse: dict[str, str] = {}
        monkeypatch.setattr(fleet_service, "status_of", self.status_of)
        monkeypatch.setattr(fleet_service, "restart", self.restart)

    def status_of(self, agent: FleetAgent) -> FleetAgentStatus:
        state, detail = self.states[agent.label]
        return FleetAgentStatus(agent=agent, state=state, detail=detail or None)

    def restart(self, project: ProjectInfo, label: str, **kwargs: Any) -> object:
        if label in self.refuse:
            raise fleet_service.FleetError(self.refuse[label])
        self.restarted.append((label, kwargs.get("agent_id")))
        return object()


def test_the_reopen_reattaches_the_running_and_restarts_only_the_gone(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents = [
        _agent(project, "coder-1", "%1"),
        _agent(project, "coder-2", "%2"),
        _agent(project, "reviewer-1", "%3"),
    ]
    fleet = _Fleet(
        monkeypatch,
        {
            "coder-1": ("waiting", ""),
            "coder-2": ("lost", "pane gone"),
            "reviewer-1": ("unknown", "tmux could not be asked"),
        },
    )
    monkeypatch.setattr(updates, "__version__", "0.9.0")
    updates.record_press(agents)
    monkeypatch.setattr(updates, "__version__", "0.9.1")

    outcome = updates.reattach_after_update()

    assert fleet.restarted == [("coder-2", "agt_coder-2")], "pinned to the recorded row"
    assert outcome is not None and outcome.updated
    assert outcome.kept == ("coder-1", "reviewer-1"), "unknown is no pane shown gone"
    assert (
        outcome.toast()
        == "✓ aisquare 0.9.1 — 2 agents reattached, 1 restarted (coder-2: pane gone)"
    )
    assert updates.reattach_after_update() is None, "an ordinary start after it finds no record"


def test_every_agent_kept_says_so_and_names_no_restart(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents = [_agent(project, "coder-1", "%1"), _agent(project, "coder-2", "%2")]
    fleet = _Fleet(monkeypatch, {"coder-1": ("working", ""), "coder-2": ("waiting", "")})
    monkeypatch.setattr(updates, "__version__", "0.9.0")
    updates.record_press(agents)
    monkeypatch.setattr(updates, "__version__", "0.9.1")

    outcome = updates.reattach_after_update()

    assert fleet.restarted == []
    assert outcome is not None
    assert outcome.toast() == "✓ aisquare 0.9.1 — 2 agents reattached"


def test_a_restart_that_fails_is_named_and_its_row_left(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents = [_agent(project, "coder-1", "%1"), _agent(project, "coder-2", "%2")]
    fleet = _Fleet(monkeypatch, {"coder-1": ("exited", "exit 1"), "coder-2": ("waiting", "")})
    fleet.refuse["coder-1"] = "cannot restart 'coder-1': its task is done"
    monkeypatch.setattr(updates, "__version__", "0.9.0")
    updates.record_press(agents)
    monkeypatch.setattr(updates, "__version__", "0.9.1")

    outcome = updates.reattach_after_update()

    assert outcome is not None and outcome.restarted == ()
    assert outcome.failed == (("coder-1", "exit 1; cannot restart 'coder-1': its task is done"),)
    assert outcome.toast() == (
        "✓ aisquare 0.9.1 — 1 agent reattached, not restarted (coder-1: exit 1; cannot restart "
        "'coder-1': its task is done)"
    )


def test_a_failed_update_reopens_on_the_old_version_and_says_so(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents = [_agent(project, "coder-1", "%1")]
    _Fleet(monkeypatch, {"coder-1": ("waiting", "")})
    monkeypatch.setattr(updates, "__version__", "0.9.0")
    updates.record_press(agents)

    outcome = updates.reattach_after_update()

    assert outcome is not None and not outcome.updated
    assert outcome.toast() == (
        "aisquare 0.9.0 is still installed: the update did not move it — 1 agent reattached"
    )
