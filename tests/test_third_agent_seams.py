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
(its reader, B7); and asserts that the files naming it are its own of the eight the contract
lists (``test_a_new_agent_touches_the_eight_files_the_contract_lists``).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from dataclasses import replace
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
from aisquare.services import claude_accounts as accounts_service
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

    def usage(
        self, account: ClaudeAccount, *, now: Any, fetch: object, fresh: bool = False
    ) -> ClaudeUsage:
        self.asked.append(account.slot)
        reading = super().usage(account, now=now, fetch=fetch, fresh=fresh)
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


# --- 092 rows 6 and 35: an SSH start and a quiet refresh, by what its accounts declare ------


class _InTheKeychain(GeminiShapedAccounts):
    """Its accounts, as an agent whose login is kept in the macOS Keychain."""

    keychain_login = True


@pytest.mark.parametrize("keychain", [False, True], ids=["login-a-file", "login-in-keychain"])
def test_from_ssh_it_starts_the_fleets_server_as_its_keychain_login_declares(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keychain: bool
) -> None:
    """SCOPE #113 092 row 6: an SSH login on macOS starts the fleet's tmux server for an agent
    whose accounts slot declares its login no Keychain item (``keychain_login`` False, the
    Gemini-shaped agent's ``oauth_creds.json``), and marks it. The agent then joins that
    server too. The same agent declaring the Keychain is refused there, by that one fact."""
    from aisquare.core import login_session
    from aisquare.core.config import FleetRoleSettings, load_config, save_config
    from aisquare.core.orchestrator import team_project
    from aisquare.services import fleet as fleet_service
    from tests import fakebin
    from tests.test_fleet_service import FakeTmux

    adapter = GeminiShapedAdapter()
    if keychain:
        adapter.accounts = _InTheKeychain()
    monkeypatch.delenv(adapter.home_env, raising=False)
    register(adapter)
    try:
        config = load_config()
        config.fleet.roles["coder"] = FleetRoleSettings(worktree=False)
        save_config(config)
        bin_dir = tmp_path / "bin"
        fakebin.executable_fake(bin_dir, "gemini", windows="set /p line=", posix="read line")
        fakebin.prepend_to_path(bin_dir, monkeypatch)
        tmux = FakeTmux()
        tmux.running = False  # no fleet server runs
        monkeypatch.setattr(fleet_service, "server", lambda config=None: tmux)
        monkeypatch.setattr(login_session, "remote_session", lambda: True)  # an SSH login
        project = team_project(tmp_path)

        if keychain:
            with pytest.raises(fleet_service.FleetError, match="start asq on the Mac first"):
                fleet_service.spawn(project, "coder", agent=adapter.id)
            assert not tmux.running and tmux.spawned == []
            return
        first = fleet_service.spawn(project, "coder", agent=adapter.id).agent
        assert tmux.running and first.pane_id in tmux.facts
        assert tmux.options == {fleet_service.SSH_STARTED_OPTION: "1"}
        second = fleet_service.spawn(project, "coder", agent=adapter.id).agent
        assert second.pane_id in tmux.facts, "it joins the server an SSH login started"
    finally:
        unregister(adapter.id)


class _InItsOwnItem(GeminiShapedAccounts):
    """Its accounts, as an agent whose login is a Keychain item it names itself, one per
    slot, and whose usage is a request (``network_usage``) keyed by that login's fingerprint.
    ``locked`` stands for this process's Keychain being locked, as an SSH login's is: its
    login and its usage then read nothing, and it says why in its own words."""

    keychain_login = True
    network_usage = True

    def __init__(self) -> None:
        self.locked = False
        self.asked: list[int] = []

    def keychain_item(self, account: ClaudeAccount) -> str | None:
        return f"gemini-shaped-oauth-{account.slot}"

    def login_fingerprint(self, account: ClaudeAccount) -> str | None:
        if self.locked:
            raise accounts_core.LoginUnreadable("its Keychain is locked", problem="locked")
        return super().login_fingerprint(account)

    def usage(
        self, account: ClaudeAccount, *, now: Any, fetch: object, fresh: bool = False
    ) -> ClaudeUsage:
        from aisquare.models import UsageCause

        if self.locked:
            return ClaudeUsage(
                available=False,
                reason="its Keychain is locked",
                cause=UsageCause.keychain_locked_ssh,
            )
        self.asked.append(account.slot)
        reading = super().usage(account, now=now, fetch=fetch, fresh=fresh)
        return reading.model_copy(update={"fetched_at": None})  # taken now, as a request is


class _InNoItem(_InItsOwnItem):
    """The same, declaring its login in the Keychain but naming no item for it."""

    def keychain_item(self, account: ClaudeAccount) -> str | None:
        return None


@pytest.mark.parametrize("item", [True, False], ids=["its-own-item", "no-item-named"])
def test_its_keychain_rows_are_on_the_item_its_accounts_name(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, item: bool
) -> None:
    """r36 finding 2: an agent whose login is in the Keychain (``keychain_login``) has its
    slots' Keychain rows on the item its accounts slot names (``keychain_item``), never on
    Claude Code's, whose name is hashed from a ``CLAUDE_CONFIG_DIR`` its launch has none of
    (the KeyError of seq 10355). On the Mac, ``read_usage`` and the cached read take its
    reading, and the row on its own item says the login it holds (its
    ``login_fingerprint``). In an SSH login, where its Keychain is locked, the cached read
    serves the Mac's reading for that login, marked; and ``accounts remove`` drops the row.
    An agent that names no item has no row, and is read as one whose login is a file."""
    monkeypatch.setattr(accounts_core, "keychain_platform", lambda: True)  # the Mac's rows
    adapter = GeminiShapedAdapter()
    slot_answers = _InItsOwnItem() if item else _InNoItem()
    adapter.accounts = slot_answers
    monkeypatch.delenv(adapter.home_env, raising=False)
    register(adapter)
    try:
        slot = _signed_in_slot(adapter)
        _quotas_till_tomorrow(slot)
        fingerprint = slot_answers.login_fingerprint(slot)
        assert fingerprint is not None
        row = f"usage-login:gemini-shaped-oauth-{slot.slot}\t{accounts_core.keychain_account()}"

        read = accounts_service.read_usage([slot])[slot.slot]
        tick = accounts_service.cached_usage_with_trends([slot])[slot.slot][0]

        assert read.available and tick.available, (read.reason, tick.reason)
        rows = _login_rows()
        if not item:
            assert rows == {}, "it names no item: no row says its login"
            assert accounts_service._logins([slot], tick.fetched_at or _now()).tokens == {
                slot.slot: fingerprint
            }, "its readings are served for its fingerprint, as a file login's are"
            return
        assert list(rows) == [row], "its own item's row, and no Claude Code item's"
        assert accounts_service.SeenLogin.load(rows[row]) == accounts_service.SeenLogin(
            token=fingerprint
        )

        slot_answers.locked = True  # an SSH login into the Mac: its Keychain is locked here
        with accounts_service._NOTED_LOCK:
            accounts_service._ROWS.clear()
        served = accounts_service.cached_usage_with_trends([slot])[slot.slot][0]

        assert served.available and served.recorded_elsewhere, served.reason
        assert served.limiting is not None and served.limiting.used_percent == 70.0
        assert slot_answers.asked == [slot.slot], "one request: the Mac's, which both served"

        removed = runner.invoke(app, ["accounts", "remove", str(slot.slot)])

        assert removed.exit_code == 0, removed.output
        assert _login_rows() == {}, "the removed slot's row is dropped"
    finally:
        unregister(adapter.id)


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime.now(UTC)


def _quotas_till_tomorrow(slot: ClaudeAccount) -> None:
    """Its quotas recorded in ``slot``: flash 10 %, pro 70 %, each resetting tomorrow."""
    from datetime import timedelta

    tomorrow = (_now() + timedelta(days=1)).isoformat()
    (slot.config_dir / ".gemini" / "quota.json").write_text(
        json.dumps(
            {
                "models": {
                    model: {"used_percent": used, "resets_at": tomorrow}
                    for model, used in {"gemini-2.5-flash": 10, "gemini-2.5-pro": 70}.items()
                }
            }
        ),
        encoding="utf-8",
    )


def _login_rows() -> dict[str, str]:
    """Every ``usage-login:`` row the store holds, by its key."""
    import sqlite3

    from aisquare.core import paths

    raw = sqlite3.connect(str(paths.db_path()))
    try:
        found = raw.execute("SELECT key, value FROM ui_state WHERE key LIKE 'usage-login:%'")
        return {str(key): str(value) for key, value in found}
    finally:
        raw.close()


class _RefreshedQuietly(GeminiShapedAccounts):
    """Its accounts, as an agent whose usage is read with a token that expires (a request,
    ``network_usage``, keyed to ``oauth_creds.json``, whose ``expiry_date`` it checks) and
    which renews it quietly with ``gemini auth refresh`` (``refresh_args``)."""

    network_usage = True

    def refresh_args(self) -> tuple[str, ...]:
        return ("auth", "refresh")

    def usage(
        self, account: ClaudeAccount, *, now: Any, fetch: object, fresh: bool = False
    ) -> ClaudeUsage:
        from aisquare.models import UsageCause

        expiry = (self._read(account, "oauth_creds.json") or {}).get("expiry_date", 0)
        if expiry <= now.timestamp() * 1000:
            return ClaudeUsage(
                available=False,
                reason="the stored token has expired",
                cause=UsageCause.token_expired,
            )
        return super().usage(account, now=now, fetch=fetch, fresh=fresh)


class _RefreshedOffline(_RefreshedQuietly):
    """The same accounts, reading its usage off its home (``network_usage`` False): its
    readings have no cache key, and its refresh is claimed for its ``login_fingerprint``."""

    network_usage = False


class _NoRefresh(_RefreshedQuietly):
    """The same accounts, declaring no quiet refresh: what every agent's slot is by default."""

    def refresh_args(self) -> tuple[str, ...]:
        return ()


_REAL_REFRESH_COMMAND = accounts_service._token_refresh_command
"""The real one, before ``conftest.no_token_refresh`` stubs it per test."""


@pytest.mark.parametrize(
    "accounts",
    [_RefreshedQuietly, _RefreshedOffline, _NoRefresh],
    ids=["refresh-args", "refresh-args-usage-offline", "none-declared"],
)
def test_its_stale_token_is_refreshed_quietly_by_the_command_its_accounts_declare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, accounts: type[_RefreshedQuietly]
) -> None:
    """SCOPE #113 092 row 35: a slot whose token went stale is refreshed by its own agent's
    command, its accounts slot's ``refresh_args`` run headless in the slot, and read again.
    The first run does not take, and the row reads ``token_refreshing`` with its retry a
    minute on, naming the agent in its reason and, r36 finding 6, in its fix, on the row
    and on the kept figure the Accounts page and ``accounts`` show behind it, with no other
    agent's name. The retry takes, and the slot reads its windows. r36 finding 8: an agent
    whose usage is read off its home (no ``network_usage``) is refreshed the same way, its
    refresh claimed for its ``login_fingerprint``. An agent that declares no refresh runs
    nothing, and its stale token reads as it is."""
    from datetime import UTC, datetime, timedelta

    from rich.text import Text

    from aisquare.cli import accounts as accounts_cli
    from aisquare.cli.ui.views.accounts import AccountRow
    from aisquare.models import UsageCause
    from tests import fakebin

    declared = accounts is not _NoRefresh
    adapter = GeminiShapedAdapter()
    adapter.accounts = accounts()
    monkeypatch.delenv(adapter.home_env, raising=False)
    register(adapter)
    try:
        binary = fakebin.executable_fake(tmp_path / "bin", "gemini", windows="", posix="")
        fakebin.prepend_to_path(binary.parent, monkeypatch)
        monkeypatch.setattr(accounts_service, "_token_refresh_command", _REAL_REFRESH_COMMAND)
        slot = _signed_in_slot(adapter)
        creds = slot.config_dir / ".gemini" / "oauth_creds.json"
        now = datetime.now(UTC)
        hour_td = timedelta(hours=1)
        hour = int(hour_td.total_seconds() * 1000)
        tomorrow = (now + timedelta(days=1)).isoformat()
        (slot.config_dir / ".gemini" / "quota.json").write_text(
            json.dumps(
                {
                    "models": {
                        model: {"used_percent": used, "resets_at": tomorrow}
                        for model, used in {"gemini-2.5-flash": 10, "gemini-2.5-pro": 70}.items()
                    },
                    "recorded_at": now.isoformat(),
                }
            ),
            encoding="utf-8",
        )
        login = json.loads(creds.read_text("utf-8"))
        creds.write_text(json.dumps({**login, "expiry_date": int(now.timestamp() * 1000) - hour}))
        ran: list[tuple[int, list[str]]] = []

        def refresh(account: ClaudeAccount, command: list[str]) -> bool:
            ran.append((account.slot, list(command)))
            if len(ran) > 1:  # the retry takes: a new token, hours from expiring
                expiry = int(now.timestamp() * 1000) + 8 * hour
                creds.write_text(
                    json.dumps({**login, "refresh_token": "r-two", "expiry_date": expiry})
                )
            return True

        monkeypatch.setattr(accounts_service, "_run_token_refresh", refresh)

        first = accounts_service.read_usage([slot], now=now)[slot.slot]

        if not declared:
            assert ran == [] and first.cause is UsageCause.token_expired
            return
        # Its binary as PATH found it (`install`): on Windows `shutil.which` spells the shim's
        # extension as PATHEXT does (`gemini.CMD` for the `gemini.cmd` written), so by case.
        [(refreshed, [found, *args])] = ran
        assert (refreshed, args) == (slot.slot, ["auth", "refresh"])
        assert os.path.normcase(found) == os.path.normcase(str(binary)), found
        assert first.cause is UsageCause.token_refreshing and not first.needs_login
        assert first.refresh_retry_at is not None
        assert "Gemini-shaped's refresh did not take" in (first.reason or ""), first.reason
        clock = first.refresh_retry_at.astimezone().strftime("%H:%M")
        fix = (
            f"none needed yet, Gemini-shaped refreshes the token at {clock} "
            "(Log in is offered if that fails)"
        )
        assert first.fix == fix
        # The figure the row keeps behind it, as the Accounts page and `accounts` show it.
        good = ClaudeUsage(available=True, session_percent=40.0, fetched_at=now - 2 * hour_td)
        kept = accounts_service._preferred(good, first.model_copy(update={"fetched_at": now}))
        assert kept is not None and kept.last_failed_cause is UsageCause.token_refreshing
        status = accounts_service.describe(slot)
        status.usage = kept
        row = AccountRow(status)  # its session cell's hover, unmounted: the row's own words
        row.usage = kept
        said = [
            row._cell_tooltip("session", Text("40%")) or "",
            *(line.plain for line in accounts_cli._kept_lines([status])),
        ]
        assert len(said) == 2 and all(f"fix: {fix}" in line for line in said), said
        assert not any("Claude Code" in line or "Codex" in line for line in [*said, fix]), said

        again = accounts_service.read_usage([slot], now=first.refresh_retry_at)[slot.slot]

        assert len(ran) == 2, "the retry, once due"
        assert again.available and again.limiting is not None
        assert again.limiting.used_percent == 70.0
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


@pytest.mark.parametrize("keychain", [False, True], ids=["login-a-file", "login-in-keychain"])
def test_a_switch_onto_it_in_the_server_ssh_started_asks_its_keychain_login_before_the_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keychain: bool
) -> None:
    """Review r36 of #113, finding 1, for a third agent: a Codex agent in the fleet server an
    SSH login started, switched onto it (``fleet switch --agent``), is asked of ITS accounts
    slot before the stop. Declaring its login in the Keychain (``keychain_login`` True), the
    switch is refused as Claude Code's is, and the Codex agent runs on; declaring it a file,
    it starts in that server as Codex does."""
    from aisquare.core import login_session
    from aisquare.core.config import FleetRoleSettings, load_config, save_config
    from aisquare.core.orchestrator import team_project
    from aisquare.services import fleet as fleet_service
    from tests import fakebin
    from tests.test_fleet_service import FakeTmux

    adapter = GeminiShapedAdapter()
    if keychain:
        adapter.accounts = _InTheKeychain()
    monkeypatch.delenv(adapter.home_env, raising=False)
    register(adapter)
    try:
        config = load_config()
        config.fleet.roles["coder"] = FleetRoleSettings(worktree=False)
        save_config(config)
        bin_dir = tmp_path / "bin"
        for binary in ("gemini", "codex"):
            fakebin.executable_fake(bin_dir, binary, windows="set /p line=", posix="read line")
        fakebin.prepend_to_path(bin_dir, monkeypatch)
        tmux = FakeTmux()
        tmux.running = False  # no fleet server runs
        monkeypatch.setattr(fleet_service, "server", lambda config=None: tmux)
        monkeypatch.setattr(login_session, "remote_session", lambda: True)  # an SSH login
        project = team_project(tmp_path)
        codex = fleet_service.spawn(project, "coder", agent="codex").agent
        fleet_service.spawn(project, "coder", agent="codex")  # keeps the server up
        assert tmux.options == {fleet_service.SSH_STARTED_OPTION: "1"}, "SSH started it"
        before = (list(tmux.typed), len(tmux.spawned), list(tmux.killed))

        if keychain:
            with pytest.raises(fleet_service.FleetError) as refused:
                fleet_service.switch(project, codex.label, to_agent=adapter.id)
            assert str(refused.value).startswith(
                f"cannot switch {codex.label!r}: not started: {fleet_service.SSH_STARTED_REFUSAL}"
            ), str(refused.value)
            assert (list(tmux.typed), len(tmux.spawned), list(tmux.killed)) == before
            assert codex.pane_id in tmux.facts, "the Codex agent runs on"
            return
        receipt = fleet_service.switch(project, codex.label, to_agent=adapter.id)
        assert receipt.to_agent == adapter.id and receipt.started.pane_id in tmux.facts
        assert tmux.options == {fleet_service.SSH_STARTED_OPTION: "1"}, "in that server"
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
#: Each file the contract's "The files" table lists, and where the Gemini-shaped synthetic agent
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
}
NAMES_IT = re.compile(r"gemini[-_]shaped|GeminiShaped", re.IGNORECASE)
#: The files of the contract's "Optional routes" table: an agent that takes a route (its live
#: suite in CI, a plugin, aisquare's shell installers, a mention in the prose) touches them
#: besides the eight, and no other file may name it.
OPTIONAL = (
    ".github/workflows/ci.yml",
    "plugins/<agent>/",
    ".agents/plugins/marketplace.json",
    ".claude-plugin/marketplace.json",
    ".gitattributes",
    ".github/workflows/install.yml",
    "tests/test_<agent>_plugin.py",
    "tests/test_<agent>_plugin_route.py",
    "install.sh",
    "install.ps1",
    "README.md",
    "CHANGELOG.md",
)
#: Where a file naming an agent is looked for: beside these, every file at the top.
SCANNED = ("src", "tests", ".github", "plugins", ".agents", ".claude-plugin")


def _contract() -> str:
    """The contract: the ``core.agent_adapters`` package docstring, "Adding a terminal agent"."""
    from aisquare.core import agent_adapters

    text = agent_adapters.__doc__ or ""
    assert "## Adding a terminal agent" in text, "the contract is the package docstring"
    return text


def _guide_files() -> list[str]:
    """The files the contract's "Adding a terminal agent" table lists, in its order."""
    guide = _contract()
    table = guide.split("### The files", 1)[1].split("\n\n|", 1)[1].split("\n\n", 1)[0]
    rows = [row for row in ("|" + table).splitlines() if re.match(r"\| \d", row)]
    return [path for row in rows for path in re.findall(r"`([^`]+)`", row.split("|")[2])]


def _guide_optional() -> list[str]:
    """The files the contract's "Optional routes" table lists, in its order, each once: each
    path in a code span of its Files column."""
    guide = _contract()
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


def test_a_new_agent_touches_the_eight_files_the_contract_lists() -> None:
    """Acceptance 3 of L7: the contract lists exactly the files a new terminal agent touches,
    eight, and every file that names the Gemini-shaped synthetic agent, whose launch,
    transcript, usage and info line run through the shared code above, is its own of one of
    them: none under ``src``, nothing shared edited for it."""
    listed = _guide_files()
    assert listed == list(COUNTERPARTS), listed
    assert len(listed) == 8
    assert _guide_optional() == list(OPTIONAL), _guide_optional()
    named = _files_naming_it()
    assert not {path for path in named if path.startswith("src/")}, named
    own = {path for path in named if not _optional(path)}
    assert own == {path for path in COUNTERPARTS.values() if path is not None}, named
    assert not named - own, "it takes no optional route"


def test_the_files_that_name_it_are_looked_for_in_ci_the_plugins_and_the_top_too(
    tmp_path: Path,
) -> None:
    """Review r18 of #113, findings 12 and 16: "touches these files, and no other" was
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


def test_its_sessions_are_counted_by_the_token_ledger_through_its_own_reader(
    tmp_path: Path,
) -> None:
    """``metrics ledger`` asks the transcript reader of each session's agent (#113's #17): this
    agent's ``gemini`` records are one request of 30 fresh and 70 cached input and 9 out
    (its ``thoughts`` count as output), and one call, with no line of shared code naming it."""
    from datetime import UTC, datetime

    from aisquare.models import TeamSession
    from aisquare.services import token_ledger

    adapter = GeminiShapedAdapter()
    register(adapter)
    try:
        path = tmp_path / "chat.jsonl"
        records = [
            {
                "type": "gemini",
                "id": "g1",
                "model": "gemini-x",
                "tokens": {"input": 100, "cached": 70, "output": 7, "thoughts": 2},
                "toolCalls": [{"id": "c1", "name": "run_shell_command", "args": {}}],
            }
        ]
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        now = datetime.now(tz=UTC)
        session = TeamSession(
            id="s1",
            project_id="prj_x",
            role="coder",
            started_at=now,
            last_seen_at=now,
            transcript_path=str(path),
            agent=adapter.id,
        )

        (row,) = token_ledger.ledger([session])
    finally:
        unregister(adapter.id)

    assert (row.requests, row.input_fresh, row.input_cache_read, row.output) == (1, 30, 70, 9)
    assert row.baseline == 100 and row.tool_calls == {"run_shell_command": 1}


def test_its_steering_reaches_outcomes_through_its_own_reader(tmp_path: Path) -> None:
    """``aisquare outcomes`` asks the transcript reader of each session's agent for who steered it
    (``AgentTranscript.facts``, #113's 092 row 10): this agent's records say a prompt, an edit
    (its ``write_file``), a call its dialog refused (a ``$patch`` saying ``cancelled``) and the
    human stopping it (an ``info`` record), and the week counts each, with no line of shared code
    naming it. One session is known by its board row, the other by its usage row alone: each
    names its agent, and that decides, the logs lying where Claude Code keeps its own (under a
    ``projects`` folder, which by its path alone would be read as Claude Code's)."""
    from datetime import UTC, datetime, timedelta

    from aisquare.core.ids import new_trace_id
    from aisquare.core.store import store_session
    from aisquare.models import ProjectInfo, SessionUsage, TeamSession, TurnMetric
    from aisquare.services import outcomes

    adapter = GeminiShapedAdapter()
    now = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    start = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)

    def log(name: str, edited: str) -> Path:
        at = [(start + timedelta(minutes=n)).strftime("%Y-%m-%dT%H:%M:%S.000Z") for n in range(4)]
        call = {"id": "c1", "name": "write_file", "args": {"file_path": edited}}
        records = [
            {"type": "user", "timestamp": at[0], "content": "write it, then run the tests"},
            {"type": "gemini", "timestamp": at[1], "id": "g1", "toolCalls": [call]},
            {"$patch": {"toolCalls": [{"id": "c2", "status": "cancelled"}]}, "timestamp": at[2]},
            {"type": "info", "timestamp": at[3], "content": "Request cancelled."},
        ]
        path = tmp_path / "home" / "projects" / "-work" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        return path

    register(adapter)
    try:
        with store_session() as store:
            store.ensure_project(ProjectInfo(id="prj_gem", root=tmp_path))
            store.upsert_session(
                TeamSession(
                    id="s-board",
                    project_id="prj_gem",
                    role="coder",
                    started_at=start,
                    last_seen_at=start,
                    transcript_path=str(log("board.jsonl", "/w/a.py")),
                    agent=adapter.id,
                )
            )
            store.record_usage(
                SessionUsage(
                    session_id="s-usage",
                    project_id="prj_gem",
                    transcript_path=str(log("usage.jsonl", "/w/b.py")),
                    started_at=start,
                    updated_at=start,
                    agent=adapter.id,
                ),
                None,
                previous_state=None,
            )
            for session_id in ("s-board", "s-usage"):
                store.open_turn(
                    TurnMetric(
                        trace_id=new_trace_id(),
                        project_id="prj_gem",
                        session_id=session_id,
                        started_at=start,
                        trigger="prompt_submit",
                        origin="human:typed",
                    )
                )
        result = outcomes.week_report("2026-W44", now=now, tz=UTC, use_gh=False)
    finally:
        unregister(adapter.id)

    assert result.effort.files_edited == 2
    assert (result.direction.interrupts, result.direction.denied_permissions) == (2, 2)


def test_its_facts_read_by_a_pool_that_does_not_know_it_are_read_here(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A large first read goes through a pool of spawned processes, which know the agents
    registered at import: this one, registered at run time, is read in this process instead,
    and its facts are all there (``services.transcript_facts``)."""
    from datetime import UTC, datetime

    from aisquare.services import transcript_facts

    adapter = GeminiShapedAdapter()
    paths = []
    for n in range(2):
        path = tmp_path / f"chat-{n}.jsonl"
        record = {"type": "info", "timestamp": "2026-10-27T09:00:00.000Z"}
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        paths.append(path)
    monkeypatch.setattr(transcript_facts, "PARALLEL_BYTES", 0)
    monkeypatch.setattr("aisquare.services.transcript_facts.os.cpu_count", lambda: 2)
    register(adapter)
    try:
        found = transcript_facts.read_facts(
            paths,
            since=datetime(2026, 10, 19, tzinfo=UTC),
            agents=dict.fromkeys(paths, adapter.id),
        )
    finally:
        unregister(adapter.id)

    assert [[fact.kind for fact in found[path]] for path in paths] == [["i"], ["i"]]


def test_a_sessions_other_files_are_the_ones_its_agents_reader_names(tmp_path: Path) -> None:
    """Which files are a session's is its agent's to say (``AgentTranscript.related_files``),
    never Claude Code's layout in shared code (review r36 of #113, finding 7): this agent keeps
    its sub-agents' logs in ``<session>.agents/``, and their edit and refusal are its session's
    (a sub-agent's task no prompt of the human's), while a ``<session>/subagents/`` folder
    beside it, Claude Code's, is no file of its session. Claude Code's reader names that
    folder's files, and Codex's names none."""
    from datetime import UTC, datetime

    from aisquare.core.agent_adapters import get_adapter
    from aisquare.services import transcript_facts
    from tests.gemini_shaped_agent import SUBAGENT_LOGS

    def log(path: Path, *records: dict[str, Any]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        return path

    def at(minute: int) -> str:
        return f"2026-10-27T09:{minute:02d}:00.000Z"

    def edit(minute: int, call: str, file: str) -> dict[str, Any]:
        made = {"id": call, "name": "write_file", "args": {"file_path": file}}
        return {"type": "gemini", "timestamp": at(minute), "toolCalls": [made]}

    session = log(tmp_path / "chat.jsonl", {"type": "user", "content": "go", "timestamp": at(0)})
    helper = log(
        tmp_path / f"chat{SUBAGENT_LOGS}" / "helper-1.log",
        {"type": "user", "content": "the sub-agent's task", "timestamp": at(1)},
        edit(2, "c1", "/w/sub.py"),
        {"$patch": {"toolCalls": [{"id": "c2", "status": "cancelled"}]}, "timestamp": at(3)},
        {"type": "info", "content": "stopped", "timestamp": at(4)},
    )
    claudes = log(tmp_path / "chat" / "subagents" / "agent-1.jsonl", edit(5, "c3", "/w/no.py"))
    adapter = GeminiShapedAdapter()
    reader = adapter.transcript_reader
    assert reader is not None

    register(adapter)
    try:
        assert reader.related_files(session) == [helper]
        found = transcript_facts.read_facts(
            [session], since=datetime(2026, 10, 19, tzinfo=UTC), agents={session: adapter.id}
        )
    finally:
        unregister(adapter.id)

    assert [(fact.kind, fact.detail) for fact in found[session]] == [
        ("p", "text"),
        ("e", "/w/sub.py"),
        ("d", ""),
    ]
    claude, codex = get_adapter("claude-code"), get_adapter("codex")
    assert claude.transcript_reader is not None and codex.transcript_reader is not None
    assert claude.transcript_reader.related_files(session) == [claudes]
    assert tuple(codex.transcript_reader.related_files(session)) == ()


def test_a_subagents_own_log_is_counted_in_its_session_by_its_readers_word_alone(
    tmp_path: Path,
) -> None:
    """Whether a transcript is a subagent's, and whose, is its agent's to say
    (``AgentTranscript.parent_session``), never Codex's ``session_meta`` in shared code (SMOKE12
    F2): this agent's helper thread keeps a log of its own beside its session's, its first
    record naming the session. The sweep lists both, reads the helper's into the session's
    row, its tokens once there and its task no one's prompt: one session, one human turn."""
    from datetime import UTC, datetime

    from aisquare.core.store import store_session
    from aisquare.services import outcomes, usage_stats
    from aisquare.services.agent_events import session_key

    adapter = FakeAgentAdapter()
    home = tmp_path / "fake-home"
    (home / "threads").mkdir(parents=True)

    def log(name: str, *records: dict[str, Any]) -> Path:
        path = home / "threads" / f"{name}.log"
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        return path

    def billed(minute: int, reply: str, fresh: int) -> dict[str, Any]:
        at = f"2026-10-27T09:{minute:02d}:00.000Z"
        return {"timestamp": at, "kind": "billed", "id": reply, "model": "fake-1", "fresh": fresh}

    def heard(minute: int, text: str) -> dict[str, Any]:
        return {"timestamp": f"2026-10-27T09:{minute:02d}:00.000Z", "kind": "heard", "text": text}

    log("main", heard(0, "go"), billed(1, "r1", 100))
    helper = log(
        "helper-1",
        {"timestamp": "2026-10-27T09:02:00.000Z", "kind": "helper", "of": "main"},
        heard(2, "the helper's task"),
        billed(3, "r2", 40),
    )
    register(adapter)
    try:
        reader = adapter.transcript_reader
        assert reader is not None and reader.parent_session(helper) == "main"
        found = [usage_stats.Home(adapter.accounts.plain_slot, adapter.id, home)]
        report = usage_stats.sweep(found_in=found, now=datetime(2026, 10, 29, tzinfo=UTC))
        with store_session() as store:
            rows = store.session_usages()
        result = outcomes.week_report(
            "2026-W44", now=datetime(2026, 10, 29, 12, 0, tzinfo=UTC), tz=UTC, use_gh=False
        )
    finally:
        unregister(adapter.id)

    assert (report.transcripts, report.sessions_new, report.turns) == (2, 1, 2)
    assert [row.session_id for row in rows] == [session_key(adapter, home, "main")]
    models = rows[0].usage["models"].values()
    assert sum(model["input"] for model in models) == 140, "the helper's tokens, once"
    assert (result.direction.sessions, result.direction.human_turns) == (1, 1)


def test_an_agent_that_declares_no_compact_command_is_never_compacted_from_here(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``capabilities.compact`` is what the quiet compaction types (#113's #17): this agent
    declares none, so its manager's Stop arms nothing, and a worker armed anyway refuses
    before it types, saying why. (``tests/test_quiet_compaction_per_agent.py`` runs the
    synthetic agent that does declare one.)"""
    from aisquare.core.orchestrator import team_project
    from aisquare.core.store import store_session
    from aisquare.services import delivery, hooks, quiet_compact
    from aisquare.services import fleet as fleet_service
    from aisquare.services import team as team_service
    from tests.test_quiet_compaction_per_agent import QuietManager

    project = team_project(tmp_path)
    with store_session() as store:
        store.ensure_project(project)

    adapter = GeminiShapedAdapter()
    assert adapter.capabilities.compact is None
    register(adapter)
    try:
        monkeypatch.setattr(quiet_compact, "_sleep", lambda seconds: None)
        monkeypatch.setattr(fleet_service, "quiet_prompt_refusal", lambda agent: None)
        typed: list[str] = []

        def type_line(agent: Any, text: str, *, acked: Any) -> delivery.Delivered:
            typed.append(text)
            return delivery.Delivered(delivery.TellResult("yes", "typed"), None, 1)

        monkeypatch.setattr(fleet_service, "type_line", type_line)
        armed: list[list[str]] = []
        monkeypatch.setattr(hooks, "_detach", armed.append)
        manager = QuietManager(project, tmp_path, adapter.id, adapter.binary)
        # Over the line by its own reader's count, so the missing command is what refuses.
        record = {"type": "gemini", "id": "g1", "tokens": {"input": 450_000, "output": 1}}
        manager.rollout.write_text(json.dumps(record) + "\n", encoding="utf-8")

        hooks.turn_stopped(
            project.root,
            session_id=manager.sid,
            last_message="Spawned coder-auth for #12.",
            permission_mode="auto",
            agent=adapter.id,
        )
        said = quiet_compact.check(manager.sid, manager.cursor, manager.token)
        with store_session() as store:
            mark = team_service.read_quiet_mark(store, manager.sid)
    finally:
        unregister(adapter.id)

    assert armed == [], "its Stop arms nothing"
    assert typed == [] and mark is None, "nothing was typed, no mark left"
    assert said == "its agent has no command that compacts it"


def test_its_accounts_share_what_it_declares_through_the_code_every_agent_shares(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``AgentAccounts.share_parts`` (#113's #22): this agent's ``.gemini`` holds a ``skills``
    folder and a ``GEMINI.md``, declared in its adapter alone. ``accounts share`` links each
    managed slot's entry to the plain home's, the slot's own kept beside it, ``unshare`` gives
    it back, a launch links a slot made later, and the doctor names its own row: all the
    shared code, which knows no agent's name."""
    from datetime import UTC, datetime

    from aisquare.services import claude_accounts as service
    from aisquare.services import diagnostics

    adapter = GeminiShapedAdapter()
    monkeypatch.delenv(adapter.home_env, raising=False)
    register(adapter)
    try:
        plain = _home(adapter)  # its plain `.gemini`
        (plain / "skills" / "shared").mkdir(parents=True)
        (plain / "GEMINI.md").write_text("plain context\n", encoding="utf-8")
        first, second = _signed_in_slot(adapter), _signed_in_slot(adapter)
        first_own = adapter.accounts.config_dir(first.config_dir)
        first_own.mkdir(parents=True, exist_ok=True)
        (first_own / "GEMINI.md").write_text("first's own\n", encoding="utf-8")
        now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)

        changes = service.enable_share(["skills", "context"], now=now)

        got = {(c.account.slot, c.part): (c.outcome, c.detail) for c in changes}
        assert got == {
            (first.slot, "skills"): ("linked", None),
            (second.slot, "skills"): ("linked", None),
            (first.slot, "context"): ("linked", "GEMINI.md.own-20261026T120000Z"),
            (second.slot, "context"): ("linked", None),
        }
        for slot in (first, second):
            there = adapter.accounts.config_dir(slot.config_dir)
            assert (there / "GEMINI.md").read_text(encoding="utf-8") == "plain context\n"
            assert (there / "skills").resolve() == (plain / "skills").resolve()
        assert (first_own / "GEMINI.md.own-20261026T120000Z").read_text() == "first's own\n"

        third = _signed_in_slot(adapter)
        notes = service.share_for_launch(account=third)
        assert any("now shares the plain gemini's skills" in note for note in notes), notes

        row = diagnostics._accounts_sharing_check(
            adapter.id, accounts_core.managed_accounts(adapter.id)
        )
        assert row is not None and row.name == "gemini-shaped-accounts sharing"
        assert row.status.value == "ok" and row.detail == "sharing skills, context"

        service.disable_share(["skills", "context"])
        assert (first_own / "GEMINI.md").read_text(encoding="utf-8") == "first's own\n"
        assert not (first_own / "skills").exists()
    finally:
        unregister(adapter.id)


def test_accounts_share_words_each_line_by_the_agent_it_is_about(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """``aisquare accounts share`` printed every link as "linked to the plain claude's" and every
    merged part as ``memory``: a line is its slot's agent's, with its plain home's name
    (``AgentAccounts.plain_label``) and its own merged part's (``scratch``)."""
    gemini, fake = GeminiShapedAdapter(), FakeAgentAdapter()
    for adapter in (gemini, fake):
        monkeypatch.delenv(adapter.home_env, raising=False)
        register(adapter)
    try:
        (_home(gemini) / "skills").mkdir()
        link = _signed_in_slot(gemini)
        merged = accounts_core.create_account(fake.id)
        notes = merged.config_dir / "projects" / "demo" / "notes"
        notes.mkdir(parents=True)
        (notes / "a.md").write_text("one\n", encoding="utf-8")
        again = accounts_core.create_account(fake.id)
        (again.config_dir / "projects" / "demo" / "notes").mkdir(parents=True)
        (again.config_dir / "projects" / "demo" / "notes" / "a.md").write_text(
            "two\n", encoding="utf-8"
        )

        result = runner.invoke(app, ["accounts", "share", "--enable", "skills,scratch"])

        assert result.exit_code == 0, result.output
        lines = [line.strip() for line in result.output.splitlines()]
        [linked] = [line for line in lines if f"account {link.slot} skills:" in line]
        assert linked == f"· account {link.slot} skills: linked to the plain gemini's", linked
        [merge] = [line for line in lines if " scratch: " in line]
        assert merge.startswith(f"· account {again.slot} scratch: merged (demo: a.md differed")
        assert "memory" not in result.output
    finally:
        unregister(fake.id)
        unregister(gemini.id)


def test_a_merged_part_is_merged_once_and_handed_to_a_launch_by_its_agents_own_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other kind of part (``how="merge"``): the fake agent's ``scratch`` notes. Merged from
    every slot into one directory the first time it is turned on (a file that differs kept
    under the slot's name), and handed to a launch by the agent's own flag (``--notes``), which
    the operator's own line wins over, said in one dim line."""
    from aisquare.cli import launch as launch_cli
    from aisquare.services import claude_accounts as service

    adapter = FakeAgentAdapter()
    monkeypatch.delenv(adapter.home_env, raising=False)
    register(adapter)
    try:
        first, second = (accounts_core.create_account(adapter.id) for _ in range(2))
        for slot, text in ((first, "from one\n"), (second, "from two\n")):
            notes = slot.config_dir / "projects" / "demo" / "notes"
            notes.mkdir(parents=True)
            (notes / "a.md").write_text(text, encoding="utf-8")
            (notes / f"{slot.slot}.md").write_text(text, encoding="utf-8")

        changes = service.enable_share(["scratch"])

        root = service.shared_memory_root(adapter.id) / "demo"
        assert (root / "a.md").read_text(encoding="utf-8") == "from one\n"
        assert (root / f"a.slot{second.slot}.md").read_text(encoding="utf-8") == "from two\n"
        assert (root / f"{first.slot}.md").exists() and (root / f"{second.slot}.md").exists()
        assert [(c.part, c.outcome) for c in changes] == [("scratch", "merged")], (
            "the part is the agent's own, not Claude Code's `memory`"
        )
        assert service.enable_share(["scratch"]) == [], "only the first enable merges"

        where = Path.cwd().name
        directory = service.shared_memory_directory(adapter.id, Path.cwd())
        assert directory == service.shared_memory_root(adapter.id) / where
        assert launch_cli._with_shared_memory(adapter.binary, ["--model", "m"], []) == [
            "--model",
            "m",
            "--notes",
            str(directory),
        ]
        assert launch_cli._with_shared_memory(
            adapter.binary, ["--model", "m"], ["--notes", "elsewhere"]
        ) == ["--model", "m"], "the operator's own wins"
        assert service.shared_memory_directory("claude-code", Path.cwd()) is None, (
            "another agent's merged part is off: its launch gets nothing"
        )

        service.disable_share(["scratch"])
        assert launch_cli._with_shared_memory(adapter.binary, ["--model", "m"], []) == [
            "--model",
            "m",
        ]
    finally:
        unregister(adapter.id)


def test_its_old_quota_reading_over_the_line_counts_as_full_by_the_age_of_its_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A kept reading over the line with no reset since is full whatever its age (manager
    decision, 2026-10-26): an agent that reads its usage offline has only such readings, and
    this one's age is its ``quota.json``'s ``recorded_at``. Pro at 95 % three hours ago, its
    day not over, passes its place to the slot with room, and the note says how old it is."""
    from datetime import UTC, datetime, timedelta

    from aisquare.services import claude_accounts as service

    adapter = GeminiShapedAdapter()
    monkeypatch.delenv(adapter.home_env, raising=False)
    register(adapter)
    try:
        full, roomy = _signed_in_slot(adapter), _signed_in_slot(adapter)
        now = datetime.now(UTC)

        def record(slot: ClaudeAccount, pro: float, resets: datetime) -> None:
            where = adapter.accounts.config_dir(slot.config_dir) / "quota.json"
            where.write_text(
                json.dumps(
                    {
                        "models": {
                            "gemini-2.5-pro": {"used_percent": pro, "resets_at": resets.isoformat()}
                        },
                        "recorded_at": (now - timedelta(hours=3)).isoformat(),
                    }
                ),
                encoding="utf-8",
            )

        record(full, 95.0, now + timedelta(hours=5))
        record(roomy, 10.0, now + timedelta(hours=5))

        decision = service.headroom_decision(
            [full, roomy], switch_at=85, now=now, max_age=service.PICK_MAX_AGE_SECONDS
        )
        assert decision.account is not None and decision.account.slot == roomy.slot, decision.notes
        assert any(
            "its reading from 3h ago is over the 85%-used line and no window has reset since" in n
            for n in decision.notes
        ), decision.notes
    finally:
        unregister(adapter.id)


# --- who started a turn, and how it is stopped (SCOPE #113 092, rows 11 and 2) -----------

#: Its sessions' ids, in the shape a queue names them (``launch_seal.native_id``: a UUID).
GEMINI_SESSION = "a1b2c3d4-0e0f-4a1b-8c2d-000000000092"


class _Queuing(GeminiShapedAdapter):
    """The Gemini-shaped agent with a queue for a busy session's next turn (``queue_args``)
    and a mark it sets in every process it starts, its hooks included (``process_env``); its
    ``exit_command`` is its own already, ``/quit``. A queue names the session the row's
    launch record holds, which a hook adds only once it proves it is the agent's own process,
    so it marks its tools' shells too (``tool_shell_env``), as Codex does. Nothing shared is
    edited for it."""

    capabilities = replace(
        GeminiShapedAdapter.capabilities,
        process_env="GEMINI_CLI",
        tool_shell_env="GEMINI_TOOL_SESSION",
    )

    def queue_args(self, native_session_id: str, message: str) -> list[str] | None:
        return ["queue", native_session_id, f"--message={message}"]


def _no_agents_mark(monkeypatch: pytest.MonkeyPatch, *adapters: AgentAdapter) -> None:
    """A person's shell: no fleet window, no launch, no agent's process or tool-shell mark."""
    from aisquare.core.agent_adapters import adapters as registered
    from aisquare.core.spawn import LAUNCH_AGENT_ENV

    monkeypatch.delenv("AISQUARE_FLEET_AGENT", raising=False)
    monkeypatch.delenv(LAUNCH_AGENT_ENV, raising=False)
    for adapter in (*registered(), *adapters):
        for name in (adapter.capabilities.process_env, adapter.capabilities.tool_shell_env):
            if name:
                monkeypatch.delenv(name, raising=False)


def test_a_tell_its_queue_takes_is_the_origin_of_the_turn_it_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row 11's GAP a: a ``fleet tell`` to a busy session goes through the agent's own queue
    (``AgentAdapter.queue_args``), whose text starts its next turn. Declaring the queue is
    all it takes for that turn to read the tell's (``human:tell``), as a typed tell's does:
    the queue path leaves the marker, and the agent's prompt hook takes it."""
    import subprocess

    from aisquare.core.config import FleetRoleSettings, load_config, save_config
    from aisquare.core.orchestrator import team_project
    from aisquare.core.store import store_session
    from aisquare.services import agent_events
    from aisquare.services import fleet as fleet_service
    from tests import fakebin
    from tests.test_fleet_service import FakeTmux

    adapter = _Queuing()
    monkeypatch.delenv(adapter.home_env, raising=False)
    register(adapter)
    try:
        config = load_config()
        config.fleet.roles["coder"] = FleetRoleSettings(worktree=False)
        save_config(config)
        bin_dir = tmp_path / "bin"
        fakebin.executable_fake(bin_dir, "gemini", windows="set /p line=", posix="read line")
        fakebin.prepend_to_path(bin_dir, monkeypatch)
        tmux = FakeTmux()
        monkeypatch.setattr(fleet_service, "server", lambda config=None: tmux)
        project = team_project(tmp_path)
        with store_session() as store:
            store.onboard_project(project)
        row = fleet_service.spawn(project, "coder", agent=adapter.id).agent
        mark = adapter.capabilities.process_env
        assert mark is not None
        window = tmux.spawned[-1]["env"]
        assert isinstance(window, dict)
        home = _home(adapter)

        def hook(event: str, **extra: object) -> None:
            """Its hook, run in its window (the row's variables, its own mark)."""
            for name, value in window.items():
                if name.startswith("AISQUARE_"):
                    monkeypatch.setenv(name, value)
            monkeypatch.setenv("AISQUARE_LAUNCH_ID", "launch-gemini")
            monkeypatch.setenv("AISQUARE_ROLE", "coder")  # as `aisquare launch` exports it
            monkeypatch.setenv(mark, "1")
            payload = {
                "hook_event_name": event,
                "session_id": GEMINI_SESSION,
                "cwd": str(project.root),
                "transcript_path": str(home / "tmp" / "chats" / "session.json"),
                **extra,
            }
            agent_events.handle(adapter, payload, home)

        hook("SessionStart", source="startup")
        hook("BeforeAgent", prompt="work")  # busy with a turn
        queued: list[list[str]] = []

        def run(argv: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
            queued.append(argv[1:])
            return subprocess.CompletedProcess(argv, 0, "queued\n", "")

        monkeypatch.setattr(fleet_service, "_run_native", run)
        _no_agents_mark(monkeypatch, adapter)  # told from a person's terminal

        told = fleet_service.tell(project, row.label, "rebase on main first")

        assert told.delivered is True and "queued for its next turn" in told.how, told
        assert queued == [["queue", GEMINI_SESSION, "--message=rebase on main first"]]
        hook("AfterAgent", prompt="work", stop_hook_active=False)
        hook("BeforeAgent", prompt="rebase on main first")
        with store_session() as store:
            bound = store.get_meta("launch-session:launch-gemini")
            turns = store.turn_metrics(session_id=bound)
    finally:
        unregister(adapter.id)

    assert [turn.origin for turn in turns] == ["human:tell", "human:typed"]


def test_a_tell_sent_where_it_marks_every_process_is_its_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Row 11's GAP b: a command run where an agent's ``process_env`` is set (Claude Code's
    ``CLAUDECODE``) runs under that agent, its hooks included, so a tell or a spawn it sends
    is ``agent``'s. Declaring the variable is all it takes: no literal names it."""
    from aisquare.core import prompt_origin

    adapter = _Queuing()
    mark = adapter.capabilities.process_env
    assert mark is not None
    _no_agents_mark(monkeypatch, adapter)
    monkeypatch.setenv(mark, "1")
    assert prompt_origin.origin_sender() == prompt_origin.HUMAN, "no agent declares it yet"

    register(adapter)
    try:
        assert prompt_origin.origin_sender() == prompt_origin.AGENT
        prompt_origin.mark_origin("go on", prompt_origin.TELL)
        assert prompt_origin.take_origin("go on") == "agent:tell"
        monkeypatch.delenv(mark)
        assert prompt_origin.origin_sender() == prompt_origin.HUMAN, "unset, a person's shell"
    finally:
        unregister(adapter.id)


def test_its_stop_dialog_says_its_own_exit_command(third: AgentAdapter) -> None:
    """Row 2's note for a future agent: the Stop dialog spelled ``/exit`` as text. It says
    what ``fleet stop`` types into this agent, its ``exit_command`` (``/quit`` for both
    synthetic agents), and Force skips that."""
    from datetime import UTC, datetime

    from aisquare.cli.ui.stop import StopAgentScreen
    from aisquare.models import FleetAgent, FleetAgentStatus, ProjectInfo

    assert third.exit_command != "/exit", "the premise: a command of its own"
    row = FleetAgent(
        id="agt_third_coder",
        project_id="prj_third",
        label="coder-third",
        role="coder",
        agent=third.id,
        pane_id="%1",
        tmux_socket="asq-test-third",
        cwd=Path("/w"),
        created_at=datetime.now(UTC),
    )
    where = ProjectInfo(id="prj_third", root=Path("/w"))

    question = StopAgentScreen(where, FleetAgentStatus(agent=row, state="waiting")).question()

    assert f"{third.exit_command}, then the window is killed after" in question.plain
    assert f"Force skips the {third.exit_command}." in question.plain
    assert "/exit" not in question.plain


def _a_row(adapter_id: str, label: str, role: str = "coder") -> Any:
    """A waiting row of ``adapter_id``'s, as the fleet lists one (a ``FleetAgentStatus``)."""
    from datetime import UTC, datetime

    from aisquare.models import FleetAgent, FleetAgentStatus

    row = FleetAgent(
        id=f"agt_{label}",
        project_id="prj_third",
        label=label,
        role=role,
        agent=adapter_id,
        pane_id="%1",
        tmux_socket="asq-test-third",
        cwd=Path("/w"),
        created_at=datetime.now(UTC),
    )
    return FleetAgentStatus(agent=row, state="waiting")


def test_its_agent_views_stop_says_its_own_exit_command(
    third: AgentAdapter, tmp_path: Path
) -> None:
    """#60 (tester of #58, seq 10298): the agent view's Stop tooltip spelled ``/exit`` for
    every agent. It says what ``fleet stop`` types into this one (``exit_command_for``, its
    ``exit_command``: ``/quit``)."""
    import asyncio

    from textual.app import App, ComposeResult
    from textual.widgets import Button

    from aisquare.cli.ui.views.agent import AgentView
    from aisquare.services import fleet as fleet_service
    from tests.pane_harness import FakePane, FakeTmux

    assert third.exit_command != "/exit", "the premise: a command of its own"
    fake = FakeTmux()
    fake.panes["%1"] = FakePane(screen=["ready"])
    shown = _a_row(third.id, "coder-third")
    view = AgentView(shown, server=fake.server(tmp_path), escape_key="f12", id="view")

    class Host(App[None]):
        def compose(self) -> ComposeResult:
            yield view

    async def drive() -> str:
        async with Host().run_test(size=(80, 12)) as pilot:
            await pilot.pause()
            return str(view.query_one("#agent-stop", Button).tooltip)

    tip = asyncio.run(drive())

    assert fleet_service.exit_command_for(shown.agent) == third.exit_command
    assert tip.startswith(f"{third.exit_command}, a grace period, then the window"), tip
    assert "/exit" not in tip


def _bulk_about(monkeypatch: pytest.MonkeyPatch, action: str, rows: list[Any]) -> tuple[str, str]:
    """What the bulk dialog says ``action`` does once it shows ``rows``, and with *Managers
    only* switched on."""
    import asyncio

    from textual.app import App
    from textual.widgets import Static, Switch

    from aisquare.cli.ui.views.fleet_bulk import ConfirmBulk
    from aisquare.models import ProjectInfo
    from aisquare.services import fleet as fleet_service

    where = ProjectInfo(id="prj_third", root=Path("/w"))
    plan = fleet_service.BulkPlan(
        action,  # type: ignore[arg-type]
        managers_only=False,
        rows=tuple(fleet_service.BulkRow(where, row) for row in rows),
        projects=(where,),
    )
    monkeypatch.setattr(fleet_service, "bulk_plan", lambda action, projects=None: plan)
    dialog = ConfirmBulk(action)  # type: ignore[arg-type]

    class Host(App[None]):
        def on_mount(self) -> None:
            self.push_screen(dialog)

    async def drive() -> tuple[str, str]:
        async with Host().run_test(size=(100, 40)) as pilot:
            about = dialog.query_one("#bulk-about", Static)
            for _ in range(200):
                if dialog.plan is not None:
                    break
                await pilot.pause(0.01)
            await pilot.pause()
            every = str(about.content)
            dialog.query_one("#bulk-managers", Switch).value = True
            await pilot.pause()
            return every, str(about.content)

    return asyncio.run(drive())


@pytest.mark.parametrize("action", ["stop", "pause"])
def test_its_bulk_stop_and_pause_say_its_own_exit_command(
    third: AgentAdapter, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """#60: the bulk stop and pause (0.9.2's pause, SCOPE 092 rows 3 and 18) said each agent
    is sent ``/exit``. Of agents that all share one exit command it names that one, asked of
    ``exit_command_for`` as ``fleet stop`` types it; while the plan is read, the neutral line."""
    from aisquare.cli.ui.views.fleet_bulk import OWN_EXIT, about_text

    rows = [_a_row(third.id, "manager", "manager"), _a_row(third.id, "coder-third")]

    every, managers = _bulk_about(monkeypatch, action, rows)
    reading = about_text(action, None)  # type: ignore[arg-type]

    assert f"Each one is sent {third.exit_command}" in every, every
    assert f"Each one is sent {third.exit_command}" in managers, managers
    assert f"Each one is sent {OWN_EXIT}" in reading, reading
    assert "/exit" not in every + managers + reading


@pytest.mark.parametrize("action", ["stop", "pause"])
def test_a_bulk_of_agents_whose_exit_commands_differ_spells_none(
    third: AgentAdapter, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """#60: a Claude Code coder beside a manager of the third agent's: each is sent its own
    command, so the dialog says "its own exit command" and spells neither. *Managers only*
    leaves the third agent's manager alone, whose command it then names."""
    from aisquare.cli.ui.views.fleet_bulk import OWN_EXIT
    from aisquare.core.agent_adapters import DEFAULT_AGENT, get_adapter

    claude = get_adapter(DEFAULT_AGENT)
    assert claude.exit_command != third.exit_command, "the premise: two commands"
    rows = [_a_row(third.id, "manager", "manager"), _a_row(claude.id, "coder-1")]

    every, managers = _bulk_about(monkeypatch, action, rows)

    assert f"Each one is sent {OWN_EXIT}" in every, every
    assert claude.exit_command not in every and third.exit_command not in every, every
    assert f"Each one is sent {third.exit_command}" in managers, managers


def test_no_string_of_the_cli_spells_an_agents_exit_command() -> None:
    """#60: what a stop types is the row's agent's own (``AgentAdapter.exit_command``), and a
    screen, a toast or a help line that names it asks ``fleet_service.exit_command_for``. A
    string under ``aisquare/cli`` that spells a registered agent's command says it of every
    agent, and so does Remote's page (``web/remote/app.js``, read whole). The commands are read
    off every registered adapter, never listed here.

    Code prose is left: a docstring, and a bare string statement (the note under a constant).
    A command's docstring is its help, and is read as any string is: a function decorated
    ``@<app>.command(…)`` or ``@<app>.callback(…)``, or registered by call,
    ``<app>.command("x")(fn)`` (``app.command("ui")(fleet.ui)``, ``team.note`` from
    ``app.py``), by its name, in any file of the package (tester of #60, seq 10323: 36 such
    commands were read as prose)."""
    import ast

    import aisquare.cli
    from aisquare.core.agent_adapters import adapters

    commands = {adapter.exit_command for adapter in adapters()}
    assert commands, "the premise: an agent registered"
    root = Path(aisquare.cli.__file__).parent
    trees = {
        path: ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for path in sorted(root.rglob("*.py"))
    }

    def registers(call: ast.AST) -> bool:
        """``<app>.command(…)`` or ``<app>.callback(…)``: what makes a function a command."""
        return (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr in {"command", "callback"}
        )

    registered = {
        fn.id if isinstance(fn, ast.Name) else fn.attr
        for tree in trees.values()
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and registers(node.func) and node.args
        for fn in node.args[:1]
        if isinstance(fn, ast.Name | ast.Attribute)
    }
    assert {"ui", "note", "attention", "launch", "exec_command"} <= registered, "by call"

    spelled: list[str] = []
    for path, tree in trees.items():
        prose = {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and (
                node.name in registered or any(registers(mark) for mark in node.decorator_list)
            ):
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    prose.discard(id(first.value))  # a command's docstring is its help
        spelled += [
            f"{path.relative_to(root)}:{node.lineno}: {node.value!r}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in prose
            and any(command in node.value for command in commands)
        ]
    page = root.parent / "web" / "remote" / "app.js"
    spelled += [
        f"{page.name}:{number}: {line.strip()}"
        for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), 1)
        if any(command in line for command in commands)
    ]

    assert not spelled, "\n".join(spelled)


def test_a_bulk_names_the_command_of_the_agents_it_acts_on_not_of_one_it_leaves(
    third: AgentAdapter,
) -> None:
    """#60 (tester's surviving mutant, seq 10323): the line is asked of the rows the action
    acts on (``BulkPlan.targets``), not every row read: an exited third-agent row the stop
    leaves, beside Claude Code agents it stops, leaves the line naming Claude Code's."""
    from aisquare.cli.ui.views.fleet_bulk import about_text
    from aisquare.core.agent_adapters import DEFAULT_AGENT, get_adapter
    from aisquare.models import ProjectInfo
    from aisquare.services import fleet as fleet_service

    claude = get_adapter(DEFAULT_AGENT)
    assert claude.exit_command != third.exit_command, "the premise: two commands"
    where = ProjectInfo(id="prj_third", root=Path("/w"))
    left = _a_row(third.id, "coder-third").model_copy(update={"state": "exited"})
    plan = fleet_service.BulkPlan(
        "stop",
        managers_only=False,
        rows=(
            fleet_service.BulkRow(where, _a_row(claude.id, "manager", "manager")),
            fleet_service.BulkRow(where, _a_row(claude.id, "coder-1")),
            fleet_service.BulkRow(where, left, skip="already exited"),
        ),
        projects=(where,),
    )
    assert [row.agent.agent for row in plan.targets] == [claude.id, claude.id]

    said = about_text("stop", plan)

    assert f"Each one is sent {claude.exit_command}," in said, said


def test_a_bulk_receipt_says_the_exit_command_its_stop_sent(
    third: AgentAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#60: a bulk restart whose stop took the agent down while another command ended its row
    said ``its /exit was sent``; it says the row's agent's own, the one the stop typed."""
    from aisquare.models import ProjectInfo
    from aisquare.services import fleet as fleet_service

    def ended_meanwhile(*_args: object, **_kwargs: object) -> object:
        raise fleet_service.EndedMeanwhile("ended by another command")

    monkeypatch.setattr(fleet_service, "restart", ended_meanwhile)
    where = ProjectInfo(id="prj_third", root=Path("/w"))
    row = fleet_service.BulkRow(where, _a_row(third.id, "coder-third"))

    receipt = fleet_service._bulk_one("restart", row, spawned_by="user")

    assert (receipt.outcome, receipt.why) == (
        "failed",
        f"its {third.exit_command} was sent, and another command ended it meanwhile; no "
        "replacement was started",
    )


def test_remote_is_sent_the_exit_command_of_each_agent_and_card(third: AgentAdapter) -> None:
    """#60: Remote's Stop sheet says what the machine sends (``tests/test_remote_page.py``,
    ``stopSaysTheMachinesExit``): each fleet row's ``exit_command`` (``fleet ls --json``'s
    payload, which Remote serves verbatim) and each card's, of the agent's own adapter; none
    for a card of no agent."""
    from datetime import UTC, datetime

    from aisquare.cli.fleet import agents_json
    from aisquare.models import ProjectInfo
    from aisquare.services import remote_needs

    where = ProjectInfo(id="prj_third", root=Path("/w"))
    shown = _a_row(third.id, "coder-third")
    rows = agents_json(where, [shown])["agents"]
    assert isinstance(rows, list) and len(rows) == 1
    assert rows[0]["exit_command"] == third.exit_command

    def card(agent: Any) -> dict[str, object]:
        now = datetime.now(UTC)
        return remote_needs._needs_item(
            "lost",
            "coder-third",
            project=where,
            agent=agent,
            reason="its window is gone",
            excerpt=None,
            detail={},
            since=now,
            push_after=None,
        ).needs_item_json()

    assert card(shown.agent)["exit_command"] == third.exit_command
    assert card(None)["exit_command"] is None


def test_a_cycle_with_no_project_tells_a_sandboxed_agent_no_command_its_broker_refuses(
    third: AgentAdapter,
) -> None:
    """SMOKE12, F1: listing the projects reads the whole machine's, and an agent whose tools
    run sandboxed (``capabilities.sandboxes_tools``: the fake agent's, not the Gemini-shaped
    one's) is told who can name its project instead of a command its broker refuses."""
    from datetime import UTC, datetime

    from aisquare.models import TeamSession
    from aisquare.services import team as team_service

    now = datetime.now(tz=UTC)
    session = TeamSession(
        id="cafe0000-0000-0000-0000-000000000000",
        agent=third.id,
        project_id="prj_x",
        role="coder",
        started_at=now,
        last_seen_at=now,
    )
    told = "\n".join(team_service._role_cycle(session, None))
    named = "`aisquare project list` names them" in told
    assert named is not third.capabilities.sandboxes_tools, told


def test_connect_asks_an_agent_that_trusts_hooks_to_record_its_own_trust(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SMOKE12 O3 of #113: an agent that runs a hook only once it is trusted
    (``requires_hook_trust``) is asked, through its own ``hook_trust``, to record that
    aisquare's hooks are, once ``connect`` has written them: by where it wrote each, never by
    what a command looks like (the reopen, seq 10437), so a look-alike of the user's in an
    event it does not install is named to it by no place; ``disconnect`` asks it to take every
    record back; and its readiness reads ``trusted`` off its own ``trust_in_force``."""
    from aisquare.core.agent_adapters.types import AgentCapabilities, HookPlace

    asked: list[tuple[Path, Path, list[HookPlace], list[str]]] = []

    class Record:
        def record_trust(
            self, home: Path, settings_path: Path, settings: Any, written: Any
        ) -> list[str]:
            commands = [
                settings["hooks"][at.event][at.group]["hooks"][at.handler]["command"]
                for at in written
            ]
            asked.append((home, settings_path, list(written), commands))
            return ["recorded"]

        def trust_in_force(self, home: Path, settings_path: Path, settings: Any) -> bool:
            return bool(asked and asked[-1][2])

        def trust_left(self, home: Path) -> None:
            return None

    class Trusting(GeminiShapedAdapter):
        capabilities: AgentCapabilities = replace(
            GeminiShapedAdapter.capabilities, requires_hook_trust=True
        )
        hook_trust = Record()

    adapter = Trusting()
    monkeypatch.delenv(adapter.home_env, raising=False)
    register(adapter)
    try:
        home = _home(adapter)
        look_alike = "/opt/someone-else/bin/aisquare hook event"
        assert agent_core._is_aisquare_hook_command(look_alike)
        (home / adapter.settings_name).write_text(
            json.dumps(
                {
                    "hooks": {
                        "Notification": [{"hooks": [{"type": "command", "command": look_alike}]}]
                    }
                }
            ),
            encoding="utf-8",
        )
        connected = runner.invoke(app, ["agents", "connect", adapter.id, "--config-dir", str(home)])
        assert connected.exit_code == 0, connected.output
        [(where, path, written, commands)] = asked
        assert (where, path) == (home, home / adapter.settings_name)
        assert sorted(at.event for at in written) == sorted(
            spec.event for spec in adapter.capabilities.hooks
        )
        assert look_alike not in commands and all(" hook " in command for command in commands)
        assert agent_core.integration_readiness(adapter.id, home)[0] == "trusted"

        gone = runner.invoke(app, ["agents", "disconnect", adapter.id, "--config-dir", str(home)])
        assert gone.exit_code == 0, gone.output
        assert asked[-1][2] == [], "every record taken back"
    finally:
        unregister(adapter.id)
