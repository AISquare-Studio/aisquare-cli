"""A third agent is a registration: hooks, uninstall and Welcome reach it through its own answers.

L5 of SCOPE #113 0.8.2 (board task #118): the fake (tests/fake_agent.py) and the
Gemini-shaped adapter (tests/gemini_shaped_agent.py) are registered here and nowhere else,
and nothing shared is edited for them. Each gets its hooks written in its own handler shape
and taken out again, the uninstall plan names what its homes keep with its own removal
commands (and refuses a purge one of them would undo), Welcome offers its own installers,
and first run finds, connects and reads it. The Gemini-shaped one does so with a home
variable that names the folder its config dir is in, and hook timeouts in milliseconds.

L7 (board task #120) runs the Gemini-shaped one through the rest of the shared code with no
shared file edited: a fleet spawn and its window's launch (its variables and one settings
layer, S3; ``auto`` as ``ask``, S4), its transcript's tail and pages (a later patch line,
S5), its usage and the info line (daily per-model windows, S6), and the telemetry receiver
(its reader, B7); and asserts that the files naming it are its own of the nine the guide
lists (``test_a_new_agent_touches_the_nine_files_the_guide_lists``).
"""

from __future__ import annotations

import json
import os
import re
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
from aisquare.models import ClaudeAccount, ClaudeUsage
from aisquare.services import agents as agents_service
from aisquare.services import first_run, lifecycle
from tests.fake_agent import FakeAgentAdapter
from tests.gemini_shaped_agent import GeminiShapedAccounts, GeminiShapedAdapter
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


def _signed_in_slot(adapter: GeminiShapedAdapter) -> ClaudeAccount:
    """A new slot of the Gemini-shaped agent's, signed in, its quotas recorded: its
    fixture's ``login`` (flash 10 %, pro 70 %)."""
    slot = accounts_core.create_account(adapter.id)
    for name, text in json.loads((FIXTURES / f"{adapter.id}.json").read_text("utf-8"))["accounts"][
        "login"
    ].items():
        (slot.config_dir / name).parent.mkdir(parents=True, exist_ok=True)
        (slot.config_dir / name).write_text(text, encoding="utf-8")
    return slot


def test_every_window_it_reports_is_listed_by_name_and_all_agree_on_the_one_that_limits(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """r18 finding 4: its windows are called by their models, not ``session`` and ``week``.
    ``accounts usage`` and ``accounts --usage`` list each by name with its percent USED and
    reset, the Accounts page draws each under the row, the JSON says which one limits, and
    every one of them names the window the sidebar's figure is (pro's, 70 %), as does the
    picker's line. Claude Code's and Codex's tables keep their two columns alone."""
    import asyncio

    from textual.app import App, ComposeResult
    from textual.widgets import Static

    from aisquare.cli.ui.attach import account_text
    from aisquare.cli.ui.views.accounts import AccountRow, windows_text
    from aisquare.services import claude_accounts as accounts_service
    from tests.test_ui_accounts import shown

    adapter = GeminiShapedAdapter()
    monkeypatch.delenv(adapter.home_env, raising=False)
    register(adapter)
    try:
        slot = _signed_in_slot(adapter)
        summary = accounts_service.read_fleet_headroom(agent=adapter.id)
        assert summary.limit_by_slot == {slot.slot: 70.0}, "the sidebar's figure"

        monkeypatch.setenv("COLUMNS", "200")  # a line a window, as a wide terminal shows it
        listed = runner.invoke(app, ["accounts", "usage"])
        assert listed.exit_code == 0, listed.output
        assert "windows used" in listed.output, listed.output
        flash = next(row for row in listed.output.splitlines() if "flash daily" in row)
        pro = next(row for row in listed.output.splitlines() if "pro daily" in row)
        assert "gemini-2.5-flash daily 10% · resets" in flash and "limits" not in flash
        assert "gemini-2.5-pro daily 70% · resets" in pro and "limits" in pro
        overview = runner.invoke(app, ["accounts", "list", "--usage"])
        assert overview.exit_code == 0, overview.output
        assert "windows used" in overview.output and "pro daily 70%" in overview.output

        dumped = json.loads(runner.invoke(app, ["--json", "accounts", "usage"]).output)
        usage = next(row["usage"] for row in dumped if row["account"]["slot"] == slot.slot)
        assert [w["name"] for w in usage["windows"]] == [
            "gemini-2.5-flash daily",
            "gemini-2.5-pro daily",
        ]
        assert (usage["limiting"]["name"], usage["limiting"]["used_percent"]) == (
            "gemini-2.5-pro daily",
            70.0,
        )

        status = accounts_service.describe(slot)
        reading = accounts_service.usage(slot)
        status.usage = reading
        assert account_text(status).endswith("· gemini-2.5-pro daily 70%"), account_text(status)
        text = windows_text(status, reading)
        assert text is not None
        flash_line, pro_line = text.plain.splitlines()
        assert "gemini-2.5-flash daily" in flash_line and "10%" in flash_line
        assert "gemini-2.5-pro daily" in pro_line and "70%" in pro_line
        assert "limits" in pro_line and "limits" not in flash_line

        class Row(App[None]):
            def compose(self) -> ComposeResult:
                yield AccountRow(status, id="row")

        async def drawn() -> tuple[bool, str]:
            row_app = Row()
            async with row_app.run_test(size=(200, 10)) as pilot:
                row_app.query_one(AccountRow).show(status, reading)
                await pilot.pause()
                line = row_app.query_one(f"#account-windows-{slot.slot}", Static)
                return line.display, shown(line)

        displayed, under = asyncio.run(drawn())
        assert displayed and under == text.plain

        # Claude Code's and Codex's windows are the two columns, and no line is under them.
        two = ClaudeUsage(available=True, session_percent=12.0, week_percent=40.0)
        assert two.own_windows == () and two.limiting is not None
        assert two.limiting.name == "week"
    finally:
        unregister(adapter.id)


class _AskedOverTheNetwork(GeminiShapedAccounts):
    """Its accounts, as an agent whose usage is a request (``network_usage``): each reading
    is taken now, and counted."""

    network_usage = True

    def __init__(self) -> None:
        self.asked: list[int] = []

    def usage(self, account: ClaudeAccount, *, now: Any, fetch: object) -> ClaudeUsage:
        self.asked.append(account.slot)
        reading = super().usage(account, now=now, fetch=fetch)
        return reading.model_copy(update={"fetched_at": None})  # taken now, as a request is


def test_a_network_reading_of_its_own_windows_is_cached_whole_and_has_a_doctor_row(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """r18 finding 11: the five-minute usage cache is keyed per agent and account by the
    fingerprint its accounts slot answers (``login_fingerprint``), not by Claude Code's
    credentials, and a sample keeps every window. An agent whose usage leaves the machine
    is asked once for two of the Accounts page's ticks and an ``accounts usage``, each served
    its daily per-model windows; and ``doctor --live`` has a headroom row for it."""
    from datetime import UTC, datetime, timedelta

    from aisquare.services import claude_accounts as accounts_service
    from aisquare.services import diagnostics

    adapter = GeminiShapedAdapter()
    counted = _AskedOverTheNetwork()
    adapter.accounts = counted
    monkeypatch.delenv(adapter.home_env, raising=False)
    register(adapter)
    try:
        slot = _signed_in_slot(adapter)
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        quotas = {"gemini-2.5-flash": 10, "gemini-2.5-pro": 70}
        (slot.config_dir / ".gemini" / "quota.json").write_text(
            json.dumps(
                {
                    "models": {
                        model: {"used_percent": used, "resets_at": tomorrow}
                        for model, used in quotas.items()
                    }
                }
            ),
            encoding="utf-8",
        )

        ticks = [accounts_service.cached_usage_with_trends([slot]) for _ in range(2)]
        listed = runner.invoke(app, ["--json", "accounts", "usage"])

        assert counted.asked == [slot.slot], "two ticks and a listing, one request"
        for tick in ticks:
            reading, _trend = tick[slot.slot]
            assert [w.name for w in reading.windows] == [
                "gemini-2.5-flash daily",
                "gemini-2.5-pro daily",
            ]
            assert reading.limiting is not None and reading.limiting.used_percent == 70.0
        assert listed.exit_code == 0, listed.output
        usage = next(
            row["usage"] for row in json.loads(listed.output) if row["account"]["slot"] == slot.slot
        )
        assert usage["limiting"]["name"] == "gemini-2.5-pro daily"

        assert diagnostics._agent_account_headroom_check(adapter.id) is None, "--live only"
        check = diagnostics._agent_account_headroom_check(adapter.id, live=True)
        assert check is not None and check.name == f"{adapter.id}-account-headroom"
        assert check.status.value == "ok" and "70%" in check.detail, check.detail
    finally:
        unregister(adapter.id)


# --- telemetry: its reader, not Codex's ----------------------------------------------------


def test_the_receiver_spools_its_records_as_its_own_reader_keeps_them() -> None:
    """B7: the receiver started for a Gemini-shaped launch keeps its ``gen_ai.*`` attributes
    (its ``telemetry_reader``), not Codex's, and its records name it."""
    from aisquare.core import insights, outbox
    from aisquare.core.config import load_config, save_config
    from aisquare.services import native_telemetry

    adapter = GeminiShapedAdapter()
    register(adapter)
    try:
        config = load_config()
        config.explainability.enabled = config.explainability.ship = True
        save_config(config)
        insights.reset_cache()
        attributes = {
            "gen_ai.request.model": "gemini-2.5-pro",
            "gen_ai.usage.input_tokens": "19",
            "model": "a key Codex's reader keeps",
            "prompt": "DO_NOT_SPOOL",
        }
        payload = {
            "resourceLogs": [
                {
                    "scopeLogs": [
                        {
                            "logRecords": [
                                {
                                    "eventName": "gemini_cli.api_response",
                                    "timeUnixNano": "7",
                                    "attributes": [
                                        {"key": key, "value": {"stringValue": value}}
                                        for key, value in attributes.items()
                                    ],
                                }
                            ]
                        }
                    ]
                }
            ]
        }

        assert native_telemetry.capture(payload, "gemini-launch", agent=adapter.id) == 1

        record = json.loads(outbox.pending()[0].read_text())
        assert record["agent"] == adapter.id
        native = record["native"]
        assert native["gen_ai.request.model"] == "gemini-2.5-pro"
        assert native["event.name"] == "gemini_cli.api_response"
        assert "model" not in native and "DO_NOT_SPOOL" not in json.dumps(record)
    finally:
        unregister(adapter.id)


# --- launch: its environment and a settings layer, no flag of its own ----------------------


def test_a_fleet_spawn_launches_it_by_its_variables_and_one_settings_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """S3: the Gemini-shaped agent takes no flag for its MCP server, its label, its teams or
    its update notice. A fleet spawn hands its window the label's variable and the teams'
    setting; the window's ``aisquare launch`` gives the agent the label, and its MCP server,
    teams switch and fleet setting as ONE settings layer of its launch's own, named by its
    variable, and its command line carries no flag of another agent's."""
    from importlib import import_module
    from unittest.mock import Mock

    from aisquare.core.config import FleetRoleSettings, load_config, save_config
    from aisquare.core.orchestrator import team_project
    from aisquare.services import fleet as fleet_service
    from tests import fakebin
    from tests.test_fleet_service import FakeTmux

    adapter = GeminiShapedAdapter()
    monkeypatch.delenv(adapter.home_env, raising=False)
    register(adapter)
    try:
        config = load_config()
        config.fleet.roles["coder"] = FleetRoleSettings(worktree=False)
        config.agents.mcp = True
        save_config(config)
        bin_dir = tmp_path / "bin"
        fakebin.executable_fake(bin_dir, "gemini", windows="set /p line=", posix="read line")
        fakebin.prepend_to_path(bin_dir, monkeypatch)
        tmux = FakeTmux()
        monkeypatch.setattr(fleet_service, "server", lambda config=None: tmux)
        project = team_project(tmp_path)

        receipt = fleet_service.spawn(project, "coder", agent=adapter.id)

        spawned = tmux.spawned[-1]
        command, window = spawned["command"], spawned["env"]
        assert isinstance(command, list) and isinstance(window, dict)
        label = receipt.agent.label
        assert window["CLI_TITLE"] == label
        carried = json.loads(window["AISQUARE_LAUNCH_SETTINGS"])
        assert carried == {"experimental": {"enableAgents": False}}
        assert "has no reviewer of its own" in " ".join(receipt.notes)  # S4, on the receipt

        for name in ("CLI_TITLE", "AISQUARE_LAUNCH_SETTINGS", "AISQUARE_FLEET_AGENT"):
            monkeypatch.setenv(name, window[name])
        execute = Mock()
        monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
        result = runner.invoke(app, command[command.index("launch") :])

        assert result.exit_code == 0, result.output
        argv, launched = execute.call_args.args[1], execute.call_args.args[2]
        assert Path(argv[0]).name == "gemini"
        assert not {"--mcp-config", "--mcp", "--name", "-c"} & set(argv), argv
        assert "AISQUARE_LAUNCH_SETTINGS" not in launched, "the agent never sees the carrier"
        assert launched["CLI_TITLE"] == label
        layer = json.loads(Path(launched["GEMINI_CLI_SYSTEM_SETTINGS_PATH"]).read_text("utf-8"))
        assert layer["experimental"] == {"enableAgents": False}
        assert layer["general"] == {"enableAutoUpdateNotification": False}
        server = layer["mcpServers"]["aisquare"]
        assert server["args"][-4:] == ["serve", "--stdio", "--close-after", "0"]
        assert server["env"]["AISQUARE_HOME"] == "$AISQUARE_HOME"
    finally:
        unregister(adapter.id)


def _gemini_session_settings(home: Path, env: dict[str, str], cwd: Path) -> dict[str, Any]:
    """What a Gemini CLI session reads of its settings files, layered as its
    docs/reference/configuration.md says, each over the one before: the system defaults
    (``GEMINI_CLI_SYSTEM_DEFAULTS_PATH``), the user's ``settings.json`` in its config dir, the
    project's ``.gemini/settings.json``, and the system settings, which "take precedence
    over all other settings files" (``GEMINI_CLI_SYSTEM_SETTINGS_PATH``)."""
    from aisquare.core.agent_adapters.types import merged_settings

    def read(path: str | Path | None) -> dict[str, Any]:
        return agent_core.read_json(Path(path)) if path else {}

    return merged_settings(
        read(env.get("GEMINI_CLI_SYSTEM_DEFAULTS_PATH")),
        read(home / "settings.json"),
        read(cwd / ".gemini" / "settings.json"),
        read(env.get("GEMINI_CLI_SYSTEM_SETTINGS_PATH")),
    )


def test_its_launchs_settings_layer_is_over_the_operators_own_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """Review r18 of #113, finding 5: ``settings_layer`` is a layer above the operator's, but
    the Gemini-shaped one was its system DEFAULTS, the layer under every settings file: a
    user's ``experimental.enableAgents: true`` kept its teams on despite ``[fleet]
    disable_native_agent_teams``, and their ``mcpServers.aisquare`` replaced aisquare's
    server. Its layer is its system SETTINGS file, over every other, which stands in for
    the machine's own: the slot is given the launch's environment, by which that file is
    read and kept in it, an administrator's value over aisquare's, said in a note."""
    from importlib import import_module
    from unittest.mock import Mock

    from aisquare.core.config import FleetRoleSettings, load_config, save_config
    from aisquare.core.orchestrator import team_project
    from aisquare.services import fleet as fleet_service
    from tests import fakebin
    from tests.test_fleet_service import FakeTmux

    adapter = GeminiShapedAdapter()
    monkeypatch.delenv(adapter.home_env, raising=False)
    register(adapter)
    try:
        config = load_config()
        config.fleet.roles["coder"] = FleetRoleSettings(worktree=False)
        config.agents.mcp = True
        save_config(config)
        home = _home(adapter)
        (home / "settings.json").write_text(
            json.dumps(
                {
                    "experimental": {"enableAgents": True},
                    "mcpServers": {"aisquare": {"command": "/opt/theirs/aisquare"}},
                    "ui": {"theme": "Dracula"},
                }
            ),
            encoding="utf-8",
        )
        machine = tmp_path / "etc" / "gemini-cli" / "settings.json"
        machine.parent.mkdir(parents=True)
        machine.write_text(
            json.dumps({"general": {"enableAutoUpdateNotification": True}, "security": {"x": 1}}),
            encoding="utf-8",
        )
        monkeypatch.setenv("GEMINI_CLI_SYSTEM_SETTINGS_PATH", str(machine))
        bin_dir = tmp_path / "bin"
        fakebin.executable_fake(bin_dir, "gemini", windows="set /p line=", posix="read line")
        fakebin.prepend_to_path(bin_dir, monkeypatch)
        tmux = FakeTmux()
        monkeypatch.setattr(fleet_service, "server", lambda config=None: tmux)
        project = team_project(tmp_path)
        fleet_service.spawn(project, "coder", agent=adapter.id)
        spawned = tmux.spawned[-1]
        command, window = spawned["command"], spawned["env"]
        assert isinstance(command, list) and isinstance(window, dict)
        for name in ("CLI_TITLE", "AISQUARE_LAUNCH_SETTINGS", "AISQUARE_FLEET_AGENT"):
            monkeypatch.setenv(name, window[name])
        execute = Mock()
        monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)

        result = runner.invoke(app, command[command.index("launch") :])

        assert result.exit_code == 0, result.output
        launched = execute.call_args.args[2]
        session = _gemini_session_settings(home, launched, Path.cwd())
        assert session["experimental"]["enableAgents"] is False, "the teams switch wins"
        assert session["mcpServers"]["aisquare"]["args"][-2:] == ["--close-after", "0"]
        assert session["ui"] == {"theme": "Dracula"}, "the operator's own settings stay"
        assert session["security"] == {"x": 1}, "the machine's system settings stay"
        assert session["general"]["enableAutoUpdateNotification"] is True, "theirs, not ours"
        assert "left as the administrator's" in result.output and str(machine) in result.output
        assert launched["GEMINI_CLI_SYSTEM_SETTINGS_PATH"] != str(machine), "a file of its own"
        assert json.loads(machine.read_text("utf-8"))["security"] == {"x": 1}, "never edited"
    finally:
        unregister(adapter.id)


# --- a session it names at launch, in its own flags ---------------------------------------


def test_an_agent_that_takes_its_session_id_is_handed_it_in_its_own_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review r18 of #113, finding 10: ``capabilities.assigns_session_id`` handed any agent
    Claude Code's ``--session-id <uuid>``, and the planner read its caller's choice through
    Claude Code's ``--resume``/``-r`` and ``--continue``/``-c``. The Gemini-shaped agent,
    set to take a session id by a flag of its own (``capabilities.session_flags``), is
    handed that flag, its own flags are read as its caller's choice and kept off a fleet
    row's recorded arguments, and a ``-c`` of its is no "continue"."""
    from dataclasses import replace

    from aisquare.core.agent_adapters.types import SessionFlags
    from aisquare.services import explainability
    from aisquare.services import fleet as fleet_service

    flags = SessionFlags(names="--new-session", resumes=("--resume", "-r"))

    class Naming(GeminiShapedAdapter):
        capabilities = replace(
            GeminiShapedAdapter.capabilities, assigns_session_id=True, session_flags=flags
        )

    adapter = Naming()
    register(adapter)
    try:
        monkeypatch.delenv(explainability._PIN_ENV_VAR, raising=False)
        minted = explainability.plan_session_identity("/opt/bin/gemini", [])
        assert minted.session_id is not None
        assert minted.inject_args == ("--new-session", minted.session_id), minted
        assert explainability.minted_identity(adapter).inject_args[0] == "--new-session"
        chosen = explainability.plan_session_identity("gemini", ["--new-session", "abc"])
        assert (chosen.session_id, chosen.inject_args) == ("abc", ())
        resumed = explainability.plan_session_identity("gemini", ["-r", "def"])
        assert (resumed.session_id, resumed.inject_args) == ("def", ())
        own = explainability.plan_session_identity("gemini", ["-c", "x"])
        assert own.inject_args[:1] == ("--new-session",), "its -c is no continue"
        recorded = fleet_service._without_session_choice(
            "gemini", ["--new-session", "abc", "-c", "x", "--resume", "def", "-m", "pro"]
        )
        assert recorded == ["-c", "x", "-m", "pro"]
    finally:
        unregister(adapter.id)


# --- transcript: a call's result in a later line ------------------------------------------


def test_a_calls_result_in_a_later_patch_line_reaches_its_tail_and_its_page(
    tmp_path: Path,
) -> None:
    """S5: the Gemini-shaped session log brings a call its result in a later ``$patch`` line.
    Read backwards, as the shared tail and pager read every transcript, its reader's
    ``records`` folds that line into the call: the call waits until it is there and not
    after, and the Transcript tab's page says how it ended."""
    from aisquare.services import transcript

    adapter = GeminiShapedAdapter()
    register(adapter)
    try:
        log = tmp_path / "session.jsonl"
        call = {"id": "c1", "name": "run_shell_command", "args": {"command": "make test"}}
        asked = [
            {"type": "user", "content": "test it", "timestamp": "2026-10-24T10:00:00Z"},
            {
                "type": "gemini",
                "content": "",
                "toolCalls": [call],
                "timestamp": "2026-10-24T10:00:05Z",
            },
        ]
        log.write_text("".join(json.dumps(r) + "\n" for r in asked), encoding="utf-8")

        waiting = transcript.read_transcript_tail(log, agent=adapter.id)
        assert waiting is not None and [p.tool_use_id for p in waiting.pending] == ["c1"]

        result = {"$patch": {"toolCalls": [{"id": "c1", "status": "success", "result": "ok"}]}}
        with log.open("a", encoding="utf-8") as out:
            out.write(json.dumps(result) + "\n")
            out.write(json.dumps({"type": "gemini", "content": "All green."}) + "\n")

        answered = transcript.read_transcript_tail(log, agent=adapter.id)
        assert answered is not None and answered.pending == ()
        page = transcript.read_page(log, agent=adapter.id, width=60)
        shown = "\n".join(page.lines)
        assert "run_shell_command · success" in shown and "All green." in shown
        assert "$patch" not in shown
    finally:
        unregister(adapter.id)


@pytest.mark.parametrize("cut", ["limit", "budget"])
def test_a_calls_result_on_the_newer_page_reaches_the_page_its_call_is_on(
    cut: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r18 of #113, finding 2: ``records`` folded a ``$patch`` only within one page's
    walk back from its end, so a call whose result line sat on the newer page rendered
    bare there (``run_shell_command``, not ``· success``). The pager hands the reader the
    lines past its page's end (``ahead``), which it reads on through only until it has the
    result: whether the page ends at its ``limit`` or at ``SCAN_BUDGET`` between a large
    call and its patch."""
    from aisquare.core.agent_adapters.transcripts import CHUNK
    from aisquare.services import transcript

    adapter = GeminiShapedAdapter()
    register(adapter)
    try:
        log = tmp_path / "session.jsonl"
        big = "x" * 200_000 if cut == "budget" else ""
        call = {"id": "c1", "name": "run_shell_command", "args": {"command": "make", "pad": big}}
        lines = [
            {"type": "user", "content": "test it"},
            {"type": "gemini", "content": "", "toolCalls": [call]},
            {"type": "gemini", "content": "Running." + " " * (70_000 if big else 0)},
            {"$patch": {"toolCalls": [{"id": "c1", "status": "success", "result": "ok"}]}},
            {"type": "gemini", "content": "All green."},
        ]
        log.write_text("".join(json.dumps(r) + "\n" for r in lines), encoding="utf-8")
        whole = "\n".join(transcript.read_page(log, agent=adapter.id, width=60).lines)
        assert "run_shell_command · success" in whole, "one page: the walk folds it"

        if cut == "budget":
            monkeypatch.setattr(transcript, "SCAN_BUDGET", 4 * CHUNK)
        limit = 2 if cut == "limit" else 10
        newer = transcript.read_page(log, agent=adapter.id, width=60, limit=limit)
        assert "All green." in "\n".join(newer.lines) and newer.more and newer.cursor
        assert "run_shell_command" not in "\n".join(newer.lines), "the call is on page 2"
        older = transcript.read_page(
            log, agent=adapter.id, width=60, limit=limit, before=newer.cursor
        )
        shown = "\n".join(older.lines)
        assert "run_shell_command · success" in shown, shown
        assert "All green." not in shown and not older.more, "nothing of page 1 again"
    finally:
        unregister(adapter.id)


# --- accounts: its plain slot by its number ------------------------------------------------


def test_its_negative_plain_slot_is_named_by_its_number_wherever_a_slot_is(
    third: AgentAdapter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """r18 finding 1: Claude Code's plain slot is 1, Codex's 0 and the managed ladder runs from
    2, so a third agent's plain slot is negative (the fake's -1, the Gemini-shaped one's -2).
    It is named by that number as any slot is: by ``choose``, a project default, ``accounts
    default`` and ``run``, ``team bind --account``, and a fleet spawn whose window's launch
    is handed ``--account -2``; and no alias may read as it."""
    from importlib import import_module
    from unittest.mock import Mock

    from aisquare.core.config import FleetRoleSettings, load_config, save_config
    from aisquare.core.orchestrator import team_project
    from aisquare.services import claude_accounts as accounts_service
    from aisquare.services import fleet as fleet_service
    from tests import fakebin
    from tests.test_fleet_service import FakeTmux

    assert third.accounts is not None
    plain = accounts_core.default_account(agent=third.id)
    plain.config_dir.mkdir(parents=True, exist_ok=True)
    number = str(plain.slot)
    assert plain.slot < 0, "0 and 1 are taken, 2 up is the managed ladder"
    project = team_project(tmp_path)

    with pytest.raises(ValueError, match="reads as a slot number"):
        accounts_core.normalise_alias(number)  # an alias of its number would hide it
    chosen = accounts_service.choose(number, agent=third.id).account
    assert chosen is not None and (chosen.agent, chosen.slot) == (third.id, plain.slot)
    accounts_service.set_default(number, project=project, agent=third.id)
    found = accounts_service.project_default(project, agent=third.id)
    assert found is not None and found.slot == plain.slot

    defaulted = runner.invoke(app, ["accounts", "default", number])
    assert defaulted.exit_code == 0, defaulted.output
    # Rank 1 IS the machine default (0.9.0, #406): `default <slot>` moves it to the top of the
    # one order, whatever agent it is. Whether it would be picked is the login's (a plain slot
    # with none is skipped by the preferred rung).
    assert accounts_service.list_accounts()[0].slot == plain.slot
    bound = runner.invoke(app, ["team", "bind", "coder", "--agent", third.id, "--account", number])
    assert bound.exit_code == 0, bound.output

    bin_dir = tmp_path / "bin"
    fakebin.executable_fake(bin_dir, third.binary, windows="set /p line=", posix="read line")
    fakebin.prepend_to_path(bin_dir, monkeypatch)
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.accounts"), "_exec", execute)
    opened = runner.invoke(app, ["accounts", "run", number])
    assert opened.exit_code == 0, opened.output
    assert f"as {third.accounts.plain_label}" in opened.output, opened.output

    config = load_config()
    config.fleet.roles["coder"] = FleetRoleSettings(worktree=False)
    save_config(config)
    tmux = FakeTmux()
    monkeypatch.setattr(fleet_service, "server", lambda config=None: tmux)
    receipt = fleet_service.spawn(project, "coder", agent=third.id, account=number)
    assert receipt.agent.account_slot == plain.slot
    command = tmux.spawned[-1]["command"]
    assert isinstance(command, list) and command[command.index("--account") + 1] == number
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    launched = runner.invoke(app, command[command.index("launch") :])
    assert launched.exit_code == 0, launched.output


# --- the files a new agent touches ---------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
#: Each file the guide's "The files" table lists, and where the Gemini-shaped synthetic agent
#: keeps its own of it: registered in tests alone, its adapter module is a test module and its
#: registration the suites' that hold it; it declares no screen, so it has no screens fixture;
#: and with no binary to run, this suite stands in for its native test and it has no runner.
COUNTERPARTS: dict[str, str | None] = {
    "src/aisquare/core/agent_adapters/<agent>.py": "tests/gemini_shaped_agent.py",
    "src/aisquare/core/agent_adapters/__init__.py": None,
    "tests/fixtures/agent_events/<id>.json": "tests/fixtures/agent_events/gemini-shaped.json",
    "tests/test_adapter_contract.py": "tests/test_adapter_contract.py",
    "tests/fixtures/agent_transcripts/<id>.json": (
        "tests/fixtures/agent_transcripts/gemini-shaped.json"
    ),
    "tests/fixtures/agent_screens/<id>.json": None,
    "tests/test_<agent>_native.py": "tests/test_third_agent_seams.py",
    "tests/run_<agent>_native.py": None,
    "docs/coding-agents.md": "docs/coding-agents.md",
}
NAMES_IT = re.compile(r"gemini[-_]shaped|GeminiShaped", re.IGNORECASE)
#: The files of the guide's "Optional routes" table: an agent that takes a route (its live
#: suite in CI, a plugin, aisquare's shell installers, a mention in the prose) touches them
#: besides the nine, and no other file may name it.
OPTIONAL = (
    ".github/workflows/ci.yml",
    "plugins/<agent>/",
    ".agents/plugins/marketplace.json",
    ".claude-plugin/marketplace.json",
    ".gitattributes",
    ".github/workflows/install.yml",
    "tests/test_<agent>_plugin.py",
    "tests/test_<agent>_plugin_route.py",
    "docs/<agent>-plugin.md",
    "install.sh",
    "install.ps1",
    "docs/install.md",
    "README.md",
    "CHANGELOG.md",
)
#: Where a file naming an agent is looked for: beside these, every file at the top.
SCANNED = ("src", "tests", "docs", ".github", "plugins", ".agents", ".claude-plugin")


def _guide_files() -> list[str]:
    """The files the guide's "Adding a terminal agent" table lists, in its order."""
    guide = (ROOT / "docs" / "coding-agents.md").read_text("utf-8")
    table = guide.split("### The files", 1)[1].split("\n\n|", 1)[1].split("\n\n", 1)[0]
    rows = [row for row in ("|" + table).splitlines() if re.match(r"\| \d", row)]
    return [path for row in rows for path in re.findall(r"`([^`]+)`", row.split("|")[2])]


def _guide_optional() -> list[str]:
    """The files the guide's "Optional routes" table lists, in its order, each once: each path
    in a code span of its Files column."""
    guide = (ROOT / "docs" / "coding-agents.md").read_text("utf-8")
    table = guide.split("### Optional routes", 1)[1].split("\n\n|", 1)[1].split("\n\n", 1)[0]
    rows = [row for row in ("|" + table).splitlines()[2:] if row.startswith("| ")]
    spans = [span for row in rows for span in re.findall(r"`([^`]+)`", row.split("|")[2])]
    paths = [span for span in spans if " " not in span and ("/" in span or "." in span)]
    return list(dict.fromkeys(paths))


def _optional(path: str) -> bool:
    """Whether ``path`` is a file of an optional route (:data:`OPTIONAL`, ``<agent>`` any name
    and a folder all it holds)."""
    from fnmatch import fnmatchcase

    patterns = [re.sub(r"<[a-z]+>", "*", route) for route in OPTIONAL]
    return any(fnmatchcase(path, f"{p}*" if p.endswith("/") else p) for p in patterns)


def _files_naming_it(root: Path = ROOT) -> set[str]:
    """Every file under :data:`SCANNED` and at the top of the repository ``root`` that names
    the Gemini-shaped agent, by path relative to it."""
    found: set[str] = set()
    paths = [path for top in SCANNED for path in (root / top).rglob("*")]
    for path in [*paths, *root.iterdir()]:
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        try:
            text = path.read_text("utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if NAMES_IT.search(text):
            found.add(path.relative_to(root).as_posix())
    return found


def test_a_new_agent_touches_the_nine_files_the_guide_lists() -> None:
    """Acceptance 3 of L7: the guide lists exactly the files a new terminal agent touches,
    nine, and every file that names the Gemini-shaped synthetic agent, whose launch,
    transcript, usage and info line run through the shared code above, is its own of one of
    them: none under ``src``, nothing shared edited for it."""
    listed = _guide_files()
    assert listed == list(COUNTERPARTS), listed
    assert len(listed) == 9
    assert _guide_optional() == list(OPTIONAL), _guide_optional()
    named = _files_naming_it()
    assert not {path for path in named if path.startswith("src/")}, named
    own = {path for path in named if not _optional(path)}
    assert own == {path for path in COUNTERPARTS.values() if path is not None}, named
    assert not named - own, "it takes no optional route"


def test_the_files_that_name_it_are_looked_for_in_ci_the_plugins_and_the_top_too(
    tmp_path: Path,
) -> None:
    """Review r18 of #113, findings 12 and 16: "touches these nine files, and no other" was
    false for an agent with a plugin route or a CI job, and the check scanned only ``src``,
    ``tests``, ``docs`` and the top's ``.md`` and ``.toml``, never ``.github`` or
    ``plugins``. It finds the agent named there and in any file at the top, and of those only
    an optional route's are allowed."""
    planted = {
        ".github/workflows/ci.yml": True,
        ".github/workflows/install.yml": True,
        ".github/dependabot.yml": False,
        "plugins/gemini-shaped/hooks/hooks.json": True,
        ".agents/plugins/marketplace.json": True,
        ".gitattributes": True,
        "install.sh": True,
        "Makefile": False,
    }
    for name in planted:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("run the gemini-shaped agent\n", encoding="utf-8")

    named = _files_naming_it(tmp_path)

    assert named == set(planted), named
    assert {path for path in named if not _optional(path)} == {
        path for path, allowed in planted.items() if not allowed
    }


def test_a_third_agents_account_on_top_of_the_one_order_runs_it(
    third: Any, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner, PR #113: a launch that nothing pins runs the agent of the best account in the
    one order across agents — a future agent's too, for implementing only its account
    slots. Its account on top, it runs; below a Claude account with room, Claude Code does;
    and the hand-off's cross-agent pick (``choose_any``) finds it among the agents allowed."""
    import shutil

    from aisquare.core.store import store_session
    from aisquare.services import agent_launch
    from aisquare.services import claude_accounts as accounts_service
    from tests.test_claude_accounts import _sign_in as claude_sign_in

    real_which = shutil.which
    installed = {"claude", third.binary}
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, *a, **k: f"/bin/{name}" if name in installed else real_which(name, *a, **k),
    )
    monkeypatch.setattr(
        accounts_service,
        "cached_usage",
        lambda accounts, **_: {
            a.slot: ClaudeUsage(available=False, reason="unread") for a in accounts
        },
    )
    with store_session():
        pass
    claude = accounts_core.create_account()
    claude_sign_in(claude, "c@example.com")
    if isinstance(third, GeminiShapedAdapter):
        mine = _signed_in_slot(third)
    else:
        mine = accounts_core.create_account(third.id)
        (mine.config_dir / "login.json").write_text(
            json.dumps({"user": "f@example.com", "token": "t"}), encoding="utf-8"
        )

    assert agent_launch.resolve("coder").adapter.id == accounts_core.AGENT, "Claude first"

    accounts_service.move(str(mine.slot), "top")
    selected = agent_launch.resolve("coder")
    assert (selected.adapter.id, selected.source) == (third.id, "accounts")

    picked = accounts_service.choose_any(agents=[third.id])
    assert picked.agent == third.id and picked.account is not None
    assert picked.account.slot == mine.slot  # the one candidate, taken with no reading
