"""The doctor's Codex row on the plugin route (t7's draft, taken into t6's rows).

What each state reads, per Codex home: the plugin waiting for /hooks trust; connected
through the plugin, naming what it runs; both routes (the plugin standing down beside
hooks.json's hooks: keep one, with disconnect or `codex plugin remove`); the plugin
running in place of hooks.json hooks whose aisquare is gone. Fixtures: what Codex 0.162.1
writes (tests/test_codex_plugin_route.py).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from aisquare.core import agents as agent_core
from aisquare.models import CheckStatus, DoctorCheck
from aisquare.services import diagnostics
from tests.test_codex_plugin_route import _version, codex, install, no_codex_home  # noqa: F401

posix_route = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the plugin route runs sh: native Windows reads only the hooks.json route",
)


@pytest.fixture(autouse=True)
def plugin_runs_this_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """The aisquare the plugin's launcher finds is this install, whatever this PATH holds."""
    monkeypatch.setattr(agent_core, "plugin_runner", agent_core.current_install)


def _codex_rows() -> list[DoctorCheck]:
    return [check for check in diagnostics._check_other_agents() if check.name == "codex"]


@posix_route
def test_doctor_asks_for_the_trust_codex_waits_for(codex: Path) -> None:  # noqa: F811
    install(codex)

    [row] = _codex_rows()

    assert row.status is CheckStatus.warn
    assert row.detail.startswith(f"Codex: unverified in {codex}")  # install.sh's trust row
    assert f"the aisquare plugin {_version()} has hooks" in row.detail
    assert "has not trusted" in row.detail
    assert row.fix is not None and "/hooks" in row.fix
    assert "agents connect" not in row.fix, "connecting would install hooks.json's beside it"


@posix_route
def test_doctor_reads_a_trusted_plugin_as_connected(codex: Path) -> None:  # noqa: F811
    install(codex, trusted=None)

    [row] = _codex_rows()

    assert row.status is CheckStatus.ok, row
    assert row.detail == (
        f"Codex: connected through the aisquare plugin {_version()} in {codex}, which runs "
        "this install"
    )


@posix_route
def test_doctor_names_what_a_trusted_plugin_cannot_run(
    codex: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(codex, trusted=None)
    monkeypatch.setattr(agent_core, "plugin_runner", lambda: None)
    monkeypatch.setattr(agent_core, "launcher_finds", lambda name: None)

    [row] = _codex_rows()

    assert row.status is CheckStatus.warn
    assert "finds neither aisquare nor uvx" in row.detail
    assert row.fix is not None and "uv" in row.fix


@posix_route
def test_doctor_says_both_routes_and_lets_the_operator_keep_one(codex: Path) -> None:  # noqa: F811
    """The plugin stands down beside hooks.json's hooks, so nothing doubles; but the two
    drift apart, and which to keep is the operator's call: a fix that is not a button."""
    install(codex, trusted=None)
    assert agent_core.install_hooks("codex", codex)

    rows = _codex_rows()

    two_ways = rows[0]
    assert two_ways.status is CheckStatus.warn
    assert f"runs aisquare two ways in {codex}" in two_ways.detail
    assert two_ways.fix is not None
    assert f"aisquare agents disconnect codex --config-dir {codex}" in two_ways.fix
    assert "codex plugin remove aisquare@aisquare-cli" in two_ways.fix
    assert len(rows) == 2, "hooks.json's hooks, which every event runs, are graded too"


@posix_route
def test_doctor_says_the_plugin_runs_in_place_of_hooks_that_cannot_start(codex: Path) -> None:  # noqa: F811
    install(codex, trusted=None)
    command = f"{codex.parent}/gone/aisquare hook codex"
    hooks = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": command}]}]}}
    (codex / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")

    [row] = _codex_rows()

    assert row.status is CheckStatus.warn
    assert "cannot start, so they fail on every event" in row.detail
    assert f"the aisquare plugin {_version()} runs in their place" in row.detail
    assert row.fix is not None and "aisquare agents disconnect codex" in row.fix


def test_doctor_is_unchanged_without_the_plugin(codex: Path) -> None:  # noqa: F811
    """The control: hooks.json alone reads as it did, with no plugin row."""
    assert agent_core.install_hooks("codex", codex)

    rows = _codex_rows()

    assert all("plugin" not in row.detail and "two ways" not in row.detail for row in rows)
