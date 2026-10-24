"""A third agent is a registration: hooks, uninstall and Welcome reach it through its own answers.

L5 of SCOPE #113 0.8.2 (board task #118): the fake (tests/fake_agent.py) and the
Gemini-shaped adapter (tests/gemini_shaped_agent.py) are registered here and nowhere else,
and nothing shared is edited for them. Each gets its hooks written in its own handler shape
and taken out again, the uninstall plan names what its homes keep with its own removal
commands (and refuses a purge one of them would undo), Welcome offers its own installers,
and first run finds, connects and reads it. The Gemini-shaped one does so with a home
variable that names the folder its config dir is in, and hook timeouts in milliseconds.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.cli.ui.views.welcome import claude_text
from aisquare.core import agents as agent_core
from aisquare.core import claude_accounts as accounts_core
from aisquare.core.agent_adapters import register, unregister
from aisquare.core.agent_adapters.types import AgentAdapter
from aisquare.core.selfcli import CliResult
from aisquare.services import agents as agents_service
from aisquare.services import first_run, lifecycle
from tests.fake_agent import FakeAgentAdapter
from tests.gemini_shaped_agent import GeminiShapedAdapter
from tests.test_first_run import admitted
from tests.test_lifecycle_uninstall import (  # noqa: F401  (fixtures: pytest finds them by name)
    Tool,
    World,
    _initialised,
    _one_object,
    default_home,
    tool,
    user_home,
    world,
)

FIXTURES = Path(__file__).parent / "fixtures" / "agent_events"


@pytest.fixture(params=[FakeAgentAdapter, GeminiShapedAdapter], ids=lambda cls: cls.id)
def third(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    adapter = request.param()
    monkeypatch.delenv(adapter.home_env, raising=False)
    register(adapter)
    try:
        yield adapter
    finally:
        unregister(adapter.id)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _home(adapter: AgentAdapter) -> Path:
    """The agent's default config dir, under the suite's agent home (``~/.gemini``)."""
    home = adapter.config_dir(agent_core.home(), {})
    home.mkdir(parents=True, exist_ok=True)
    return home


def _keeps_aisquare(adapter: AgentAdapter, home: Path) -> None:
    """``home`` keeps what the adapter's fixture says: aisquare's MCP server run through uvx."""
    for name, text in json.loads((FIXTURES / f"{adapter.id}.json").read_text("utf-8"))["lasting"][
        "files"
    ].items():
        (home / name).parent.mkdir(parents=True, exist_ok=True)
        (home / name).write_text(text, encoding="utf-8")


def _handlers(adapter: AgentAdapter, home: Path) -> list[dict[str, Any]]:
    settings = json.loads((home / adapter.settings_name).read_text(encoding="utf-8"))
    return [
        item
        for groups in settings["hooks"].values()
        for group in groups
        for item in group["hooks"]
        if agent_core._is_aisquare_hook_command(item["command"])
    ]


# --- hooks ---------------------------------------------------------------------------------


def test_connect_writes_the_agents_own_handlers_and_disconnect_takes_them_out(
    third: AgentAdapter, runner: CliRunner
) -> None:
    home = _home(third)

    connected = runner.invoke(app, ["agents", "connect", third.id, "--config-dir", str(home)])

    assert connected.exit_code == 0, connected.output
    handlers = _handlers(third, home)
    assert len(handlers) == len(third.capabilities.hooks)
    by_command = {spec.event: spec for spec in third.capabilities.hooks}
    for handler in handlers:
        event = handler["command"].split("--event ", 1)[1].split()[0]
        assert handler == third.hook_entry(by_command[event], handler["command"])
    if isinstance(third, GeminiShapedAdapter):
        # Its timeouts are milliseconds: SessionStart's 60 s is written 60000.
        start = next(h for h in handlers if "--event SessionStart" in h["command"])
        assert start["timeout"] == 60_000 and start["name"] == "aisquare"
    assert agent_core.hooks_installed(third.id, home)

    gone = runner.invoke(app, ["agents", "disconnect", third.id, "--config-dir", str(home)])

    assert gone.exit_code == 0, gone.output
    assert agent_core.hook_commands(third.id, home) == []


# --- uninstall -----------------------------------------------------------------------------


def test_uninstall_names_what_its_homes_keep_with_its_own_removals(
    third: AgentAdapter,
    tool: Tool,  # noqa: F811
    world: World,  # noqa: F811
    default_home: None,  # noqa: F811
    user_home: Path,  # noqa: F811
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    """The plan reads every home of the agent's (its default, its account slots, each as the
    config dir it reads there) through its own adapter: its hooks, its MCP server with the
    removal its own binary runs, a purge that server would undo refused, and its account
    homes, which a purge deletes. A plain run takes its hooks out and names the rest."""
    _initialised(runner, tmp_path)
    home = _home(third)
    _keeps_aisquare(third, home)  # first: Gemini-shaped's MCP servers share its hooks' file
    agents_service.connect(third.id, home)
    slot = accounts_core.create_account(third.id)
    slot_home = accounts_core.agent_config_dir(slot)
    slot_home.mkdir(parents=True, exist_ok=True)
    agents_service.connect(third.id, slot_home)

    plan = lifecycle.uninstall_plan(purge=True)

    sites = {(site.agent, site.config_dir) for site in plan.hooks}
    assert {(third.id, home), (third.id, slot_home)} <= sites
    [server] = [entry for entry in plan.mcp if entry.agent == third.id]
    assert (server.name, server.fetches) == ("memory", True)
    assert server.remove == next(e.remove for e in third.lasting_installs(home) if e.kind == "mcp")
    assert third.binary in server.remove.split()
    assert (third.id, slot.config_dir) in plan.account_homes
    if isinstance(third, GeminiShapedAdapter):
        [extension] = [entry for entry in plan.plugins if entry.agent == third.id]
        assert extension.name == "aisquare" and "extensions uninstall" in extension.remove

    purge = runner.invoke(app, ["--json", "uninstall", "--yes", "--purge"])
    refused = _one_object(purge.stdout)
    assert (purge.exit_code, refused["error"]) == (1, "mcp_registered"), refused
    assert (
        server.remove in refused["detail"] and f"{third.label}'s next session" in refused["detail"]
    )
    assert world.events == [], "refused before anything was touched"

    plain = runner.invoke(app, ["uninstall", "--yes"])

    assert plain.exit_code == 0, plain.output
    assert agent_core.hook_commands(third.id, home) == []
    assert agent_core.hook_commands(third.id, slot_home) == []
    assert f"{third.label} owns that file — remove it: {server.remove}" in plain.stdout
    assert third.label in lifecycle.stays_installed(plan)


# --- Welcome and first run -----------------------------------------------------------------


def on_path(*names: str) -> Any:
    return lambda name: f"/opt/bin/{name}" if name in names else None


def test_welcome_offers_its_own_installers_on_each_platform(third: AgentAdapter) -> None:
    linux = first_run.install_routes("linux", agent=third.id)
    assert linux == tuple(r for r in third.install_routes if "darwin" not in r.platforms)
    mac = first_run.install_routes("darwin", agent=third.id)
    assert mac == third.install_routes
    [windows] = first_run.install_routes("win32", agent=third.id)
    assert (windows.how, windows.command) == ("inside WSL2", third.install_routes[0].command)
    # Step 2 with nothing installed names every registered agent's routes, this one's too.
    state = first_run.probe_fleet(which=on_path(), sign_in=False)
    shown = claude_text(state, platform="linux").plain
    assert f"{third.label}:" in shown and third.install_routes[0].command in shown


def test_first_run_finds_connects_and_reads_it_as_the_one_agent_here(
    third: AgentAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_core, "hook_binary_version", lambda argv, **_k: "3.2.1")
    home = _home(third)
    choice = first_run.fleet_agent(which=on_path(third.binary))
    assert (choice.agent, choice.inferred) == (third.id, True)

    state = first_run.probe_fleet(which=on_path(third.binary))
    assert state.agent == third.id and state.label == third.label and state.found
    assert state.home is not None and os.path.realpath(state.home) == os.path.realpath(home)
    assert not state.connected

    calls: list[list[str]] = []

    def run(args: Any, *, cwd: Path | None = None) -> CliResult:
        calls.append(list(args))
        return CliResult(argv=list(args), returncode=0, stdout="{}", stderr="")

    assert first_run.connect(run=run, state=state).ok
    assert calls == [["--json", "agents", "connect", third.id]]
    assert admitted(first_run.connect_fix(third.id, home.parent / "elsewhere"))

    agents_service.connect(third.id, home)
    assert first_run.probe_fleet(which=on_path(third.binary)).connected


# --- usage: windows of its own -------------------------------------------------------------


def test_its_daily_per_model_quotas_are_its_usage_and_the_info_line_shows_the_one_that_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S6: the Gemini-shaped agent's usage is one window of a day per model, read offline
    from its slot; the headroom read ranks the slot by the model nearest its quota, and the
    agent's info line shows that figure, with no shared file naming it."""
    from datetime import UTC, datetime

    from aisquare.cli.ui.views.agent import info_line
    from aisquare.models import FleetAgent, FleetAgentStatus
    from aisquare.services import claude_accounts as accounts_service

    adapter = GeminiShapedAdapter()
    monkeypatch.delenv(adapter.home_env, raising=False)
    register(adapter)
    try:
        slot = accounts_core.create_account(adapter.id)
        for name, text in json.loads((FIXTURES / f"{adapter.id}.json").read_text("utf-8"))[
            "accounts"
        ]["login"].items():
            (slot.config_dir / name).parent.mkdir(parents=True, exist_ok=True)
            (slot.config_dir / name).write_text(text, encoding="utf-8")

        reading = adapter.accounts.usage(slot, now=None, fetch=None)
        summary = accounts_service.read_fleet_headroom(agent=adapter.id)

        assert [w.name for w in reading.windows] == [
            "gemini-2.5-flash daily",
            "gemini-2.5-pro daily",
        ]
        assert reading.limiting is not None and reading.limiting.name == "gemini-2.5-pro daily"
        assert summary.limit_by_slot == {slot.slot: 70.0} and summary.usable == 1
        row = FleetAgent(
            id="agt_g",
            project_id="prj_g",
            label="coder-1",
            role="coder",
            binary=adapter.binary,
            agent=adapter.id,
            pane_id="%7",
            cwd=Path("/w"),
            account_slot=slot.slot,
            created_at=datetime.now(UTC),
        )
        line = info_line(FleetAgentStatus(agent=row, state="waiting"), None, summary.limit_by_slot)
        assert f"{accounts_core.label(slot)} · 70%" in line.plain, line.plain
    finally:
        unregister(adapter.id)
